"""屋顶/月夜泛化回归（2026-09-27，用户屋顶实测：agent 只捡阳光不种植）。

根因：执行闸门白名单漏了 scene 4。本文件钉住泛化后的行为：
屋顶=花盆平台、月夜放行、坡度自校准。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.plants import KBEntry, PlantBook
from pvz.policy import generate_candidates
from pvz import offsets as O




def _roof_board(scene=4, cards=(), zombies=(), plants=(), sun=900):
    rows = 5 if scene in (0, 1, 4, 5) else 6
    return BoardState(ok=True, sun=sun, rows=rows, cols=9, game_clock=40000,
                      scene=scene,
                      slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                      plants=list(plants), zombies=list(zombies))

class RoofPlatformTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        # 花盆不在杂交图鉴里，测试手工登记（与 bind 等价的注册）
        self.book.kb_by_name['花盆'] = KBEntry(cn='花盆', en='Flower Pot', cost=25,
                                               role='platform', tags=('platform',))
        for tid, name in ((33, '花盆'), (0, '豌豆射手'), (16, '睡莲'),
                          (421, '高冰果'), (430, '向日葵女王')):
            self.assertTrue(self.book.bind_one(tid, name))

    def board(self, scene=4, cards=(), zombies=(), plants=(), sun=900):
        rows = 5 if scene in (0, 1, 4, 5) else 6
        return BoardState(ok=True, sun=sun, rows=rows, cols=9,
                          game_clock=40000, scene=scene,
                          slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies))

    def test_scene_rows_cover_moon_night(self):
        self.assertEqual(O.SCENE_ROWS.get(4), 5)
        self.assertEqual(O.SCENE_ROWS.get(5), 5, '月夜 scene 5 应有行数定义')

    def test_roof_needs_pot_but_water_needs_lily(self):
        roof = self.board(scene=4)
        self.assertTrue(roof.platform_required(0))
        self.assertTrue(roof.can_plant(0, 0, 33, self.book), '花盆可以种上空屋顶')
        self.assertFalse(roof.can_plant(0, 1, 0, self.book), '没花盆时豌豆不能直接上屋顶')
        self.assertFalse(roof.can_plant(0, 2, 16, self.book), '睡莲不能种屋顶')
        withpot = self.board(scene=4, plants=[Plant(0, 0, 1, 33)])
        self.assertTrue(withpot.can_plant(0, 1, 0, self.book), '花盆上应能种豌豆')
        water = self.board(scene=2, cards=[])
        water.row_types = {0: 1, 1: 1, 2: 2, 3: 2, 4: 1, 5: 1}
        self.assertTrue(water.can_plant(2, 0, 16, self.book), '睡莲可以下水')
        self.assertFalse(water.can_plant(2, 1, 33, self.book), '花盆不能下水')

    def test_roof_pad_support_chains_best_wall(self):
        # 屋顶受压（僵尸 460px）：应出"花盆→高冰果"的连锁候选（墙系优先）
        b = self.board(scene=4, cards=[0, 33, 421],
                       zombies=[Zombie(0, 0, 0, x=460)], sun=900)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.supports_type is not None]
        self.assertTrue(cs, '屋顶受压应出平台候选')
        pot = next(c for c in cs if c.type_id == 33)
        self.assertEqual(pot.supports_type, 421, '花盆连锁的应是墙系高冰果')
        self.assertGreaterEqual(pot.score, 70, '受压屋顶的平台候选不应被 45 封顶')

    def test_platform_fits_terrain(self):
        roof = self.board(scene=4)
        water = self.board(scene=2)
        self.assertTrue(roof.platform_fits(0, 33, self.book))
        self.assertFalse(roof.platform_fits(0, 16, self.book))
        self.assertTrue(water.platform_fits(2, 16, self.book))
        self.assertFalse(water.platform_fits(2, 33, self.book))


if __name__ == '__main__':
    unittest.main()


class RoofDeckBindingTests(unittest.TestCase):
    """复现 20:42 屋顶局的真实卡组：bind 之后必须能出种植候选（此前全 hold）。"""

    ROOF_ORDER = ["向日葵女王", "冰瓜香蒲", "狂野机枪射手", "阳光炸弹", "樱桃辣椒",
                  "雪花寒冰菇", "高冰果", "雷果子", "回收高坚果", "Cupid魅惑菇射手",
                  "睡莲", "玉米卷香蒲", "冰冻坚果", "豌豆射手", "阳光向日葵", "阳光花盆"]
    ROOF_IDS = [86, 78, 109, 2, 20, 14, 23, 183, 161, 90, 16, 28, 101, 0, 9, 33]

    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.book.kb_by_name['花盆'] = KBEntry(cn='花盆', en='Flower Pot', cost=25,
                                               role='platform', tags=('platform',))
        rep = self.book.bind_lineup(self.ROOF_IDS, self.ROOF_ORDER)
        self.assertFalse(rep['unbound_names'], f'绑定失败: {rep["unbound_names"]}')

    def test_roof_deck_generates_plant_candidates(self):
        # 复刻 20:42 局：预置花盆(type 66)在 A/B/C，阳光 700，无僵尸
        plants = [Plant(r * 3 + c, r, c, 66) for r in range(5) for c in range(3)]
        b = BoardState(ok=True, sun=700, rows=5, cols=9, game_clock=3834, scene=4,
                       slots=[SeedSlot(i, t, 0, 0) for i, t in enumerate(self.ROOF_IDS)],
                       plants=plants, zombies=[])
        cs = [c for c in generate_candidates(b, self.book) if c.kind == 'plant']
        self.assertTrue(cs, '预置花盆上必须能出种植候选（此前全 hold 的 bug）')
        kinds = {self.book.en(c.type_id) for c in cs}
        self.assertTrue(any('Sun' in k or 'Wall' in k or 'Queen' in k for k in kinds),
                        f'候选里应有可种植物: {kinds}')

    def test_pot_card_is_plantable_on_roof(self):
        b = _roof_board(scene=4)
        self.assertTrue(b.can_plant(0, 5, 33, self.book), '阳光花盆应能种上空屋顶格')
        self.assertTrue(b.platform_fits(0, 33, self.book))
        water = _roof_board(scene=2)
        self.assertFalse(water.platform_fits(2, 33, self.book), '花盆不能下水')


class RoofLearningTests(unittest.TestCase):
    """2026-09-30 屋顶学习批次：钢刺坚果王防撞 / 白天睡觉门槛 / 僵尸真名。"""

    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        # 测试手工登记（与 mechanics_bindings 等价的测试注册）
        for tid, name in ((189, '玉米卷迫击炮'), (142, '忧郁菇投手'),
                          (46, '钢刺坚果王'), (16, '睡莲'), (33, '花盆')):
            self.book.kb_by_name.setdefault(name, KBEntry(cn=name, en=name, cost=0, role='x'))
            self.book.bind_one(tid, name)
        # 预置花盆（type 66）不在杂交图鉴，由 plant_names.json 提供
        # （role=platform）；空文件测试里按同款手工登记。
        self.book.table[66] = ('屋顶花盆', 0, 'platform')

    def test_steel_nut_king_is_recognized_as_crush_resistant(self):
        # 撞车路（冰车二爷 type 5 最近）：普通墙被 nearest_is_crush 禁掉，
        # 防撞墙（钢刺坚果王）在预置花盆格正常出候选并标注 Crush-resistant
        b = _roof_board(scene=4, cards=[46], zombies=[Zombie(0, 0, 5, x=460)],
                        plants=[Plant(0, 0, 4, 66, hp=300)])
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == 46]
        self.assertTrue(cs, '预置花盆格上防撞墙应能出候选')
        self.assertTrue(any('Crush-resistant' in c.why for c in cs),
                        '钢刺坚果王应被识别为防撞墙')

    def test_day_sleeper_skipped_on_day_map(self):
        b = _roof_board(scene=4, cards=[142], zombies=[Zombie(0, 0, 0, x=600)])
        self.book.bind_one(142, '忧郁菇投手') if self.book.kb_by_name.get('忧郁菇投手') else None
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == 142]
        self.assertFalse(cs, '白天地图不应给出睡觉蘑菇的种植候选')

    def test_zombie_names_via_catalog(self):
        import io, json as J
        b = _roof_board(scene=4, zombies=[Zombie(0, 0, 5, x=400)])
        b2 = J.dumps(__import__('pvz.serialize', fromlist=['build_state']).build_state(b, self.book), ensure_ascii=False)
        self.assertIn('冰车二爷', b2, 'state 应包含目录里的僵尸真名')


class RoofCatalogGapTests(unittest.TestCase):
    """2026-09-30 屋顶审查回归：catalog 激活、预置花盆(66)未绑定时不能瘫痪。

    sync_binding 每轮会补绑场上 66，但卡槽读取失败的轮次里 generate 会在
    未绑状态运行 —— 此时 role(66) 因 catalog 的防错绑门返回 UNKNOWN，
    placement_layer 必须用 legacy 平台表兜住（与 platform_fits 同一份认知）。
    """

    DECK = [14, 189, 2, 20, 9, 147, 160, 155, 142, 34, 33, 106, 46, 161, 233, 183]

    def setUp(self):
        # 全默认数据文件（与生产同源）+ catalog 激活 + 只绑卡槽 16 卡
        self.book = PlantBook()
        self.book.activate_catalog('classic', '3.9.9')
        for tid in self.DECK:
            self.assertTrue(self.book.bind_identity(tid), f'bind {tid} 失败')

    def test_preset_pots_usable_while_66_unbound(self):
        self.assertIsNone(self.book.kb_by_id.get(66), '前置：66 应处于未绑定状态')
        plants = [Plant(r * 3 + c, r, c, 66, hp=300) for r in range(5) for c in range(3)]
        b = BoardState(ok=True, sun=150, rows=5, cols=9, game_clock=40000, scene=4,
                       slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(self.DECK)],
                       plants=plants, zombies=[])
        self.assertTrue(b.has_platform(0, 0, self.book), '预置花盆格应被识别为有平台')
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and 'producer' in self.book.tags(c.type_id)]
        self.assertTrue(cs, '预置花盆上必须能出 producer 候选（66 未绑定时）')
        self.assertTrue(all(b.can_plant(c.row, c.col, c.type_id, self.book) for c in cs))

    def test_crush_lane_offers_pot_chain_for_steel_nut(self):
        # 撞车路且落点无预置花盆时，钢刺坚果王以"花盆+钢刺"连锁候选出现。
        # 阳光 800：经济闸（sun-cost>=300）放行非紧急墙，属富余期正常建墙。
        plants = [Plant(r * 3 + c, r, c, 66, hp=300) for r in range(5) for c in range(3)]
        b = BoardState(ok=True, sun=800, rows=5, cols=9, game_clock=40000, scene=4,
                       slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(self.DECK)],
                       plants=plants, zombies=[Zombie(0, 0, 5, x=460)])
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.supports_type == 46]
        self.assertTrue(cs, '冰车路应出"花盆+钢刺坚果王"连锁候选')
        self.assertTrue(any('Crush-resistant' in c.why for c in cs),
                        '连锁候选应保留防撞标注')

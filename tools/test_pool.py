"""Pool terrain, layered occupancy and scene-dependent placement regressions."""
import unittest
import test_strategy as fixtures
from test_strategy import SUN, PEA, WALL, Plant, Zombie
from pvz.policy import generate_candidates
from pvz.ui import Layout
from pvz.board import BoardState

PAD=390
class PoolTests(unittest.TestCase):
    board=fixtures.StrategyTests.board
    def setUp(self):
        fixtures.StrategyTests.setUp(self)
        self.book.bind_one(PAD,'睡莲')
    def pool(self,cards,plants=(),zombies=(),sun=1000):
        b=self.board(cards,zombies,plants,sun)
        b.scene=2;b.rows=6
        return b
    def test_water_requires_pad(self):
        b=self.pool([PEA],zombies=[Zombie(0,2,0,x=650)])
        self.assertFalse(any(c.kind=='plant' and c.row in (2,3) for c in generate_candidates(b,self.book)))
    def test_pad_is_prerequisite_for_water_defender(self):
        b=self.pool([PAD,PEA],zombies=[Zombie(0,2,0,x=280)])
        cs=[c for c in generate_candidates(b,self.book) if c.kind=='plant' and c.row==2]
        self.assertTrue(cs)
        self.assertTrue(all(c.type_id==PAD for c in cs))
    def test_existing_pad_allows_top_plant_not_second_pad(self):
        b=self.pool([PAD,PEA],[Plant(0,2,1,PAD)],[Zombie(0,2,0,x=280)])
        cs=generate_candidates(b,self.book)
        self.assertTrue(any(c.type_id==PEA and (c.row,c.col)==(2,1) for c in cs))
        self.assertFalse(any(c.type_id==PAD and (c.row,c.col)==(2,1) for c in cs))
    def test_full_stack_blocks_another_plant(self):
        b=self.pool([PAD,PEA],[Plant(0,2,1,PAD),Plant(1,2,1,PEA)])
        self.assertFalse(any(c.kind=='plant' and (c.row,c.col)==(2,1) for c in generate_candidates(b,self.book)))
    def test_no_pad_on_grass(self):
        self.assertFalse(any(c.type_id==PAD for c in generate_candidates(self.board([PAD]),self.book)))
    def test_pool_geometry_switch_does_not_accumulate_scaling(self):
        lay=Layout.load()
        grass=lay.cell_center(4,2)
        lay.configure_board(self.pool([]))
        pool=lay.cell_center(5,2)
        self.assertLess(pool[1],lay.client_h)
        lay.configure_board(self.pool([]))
        self.assertEqual(pool,lay.cell_center(5,2))
        lay.configure_board(self.board([]))
        self.assertEqual(grass,lay.cell_center(4,2))

    def test_old_pad_is_not_proof_of_success(self):
        from pvz.board import placement_delta
        b=self.pool([PEA],[Plant(0,2,1,PAD)])
        self.assertEqual(placement_delta(b,b,PEA,2,1),(False,[]))
        a=self.pool([PEA],b.plants+[Plant(1,2,1,PEA)])
        self.assertEqual(placement_delta(b,a,PEA,2,1),(True,[]))
        a.plants[-1].row=3
        self.assertEqual(placement_delta(b,a,PEA,2,1),(False,[(3,1)]))

    def test_custom_terrain_can_identify_water(self):
        b=self.pool([PAD,PEA],zombies=[Zombie(0,2,0,x=280)])
        b.scene=20;b.row_types={0:1,1:1,2:2,3:2,4:1,5:1}
        self.assertTrue(b.is_water(2))
        self.assertTrue(any(c.type_id==PAD for c in generate_candidates(b,self.book)))
    def test_bare_pad_is_never_a_rescue(self):
        # 2026-09-26：危急水路裸荷叶（25 阳光不攻击不阻挡）曾眼睁睁看僵尸进门。
        # 2026-09-27 修订：受压水路的荷叶候选现在**连锁种墙**（一个事务里
        # 荷叶+墙一起落地，不再裸奔）—— 所以保护对象从"分值封顶 45"改为
        # "不进 emergency 池 + 必须连锁墙系卡"。
        b=self.pool([PAD,PEA],zombies=[Zombie(0,2,0,x=110)],sun=365)
        cs=generate_candidates(b,self.book)
        pads=[c for c in cs if c.type_id==PAD]
        self.assertTrue(pads)
        self.assertTrue(all(c.supports_type is not None for c in pads if c.emergency),
                        'Only a completed compound interception can rescue; bare pads cannot')
        # 手里只有豌豆时也要链接（最优可用卡）；墙优先见 test_strategy_tips.WaterLanePadTests
        self.assertTrue(all(c.supports_type is not None and c.supports_type != PAD
                            for c in pads),
                        '受压水路的荷叶必须链接防御卡，不许裸奔')

    def test_producer_recovery_behind_walls_in_high_lane(self):
        # 实战 2026-09-26：产阳光被打到 4 株以下、全路 high 时经济断供螺旋死亡。
        # high 路只要有墙护着，就允许补种向日葵。
        # 场景：1/3/4/5 路危急（不可种），0 路僵尸在 250（high）但有墙在 col2。
        ps=[Plant(0,0,2,WALL),Plant(1,1,0,SUN),Plant(2,2,0,SUN)]
        zs=[Zombie(0,0,0,x=250)]+[Zombie(i,i,0,x=150) for i in range(1,6)]
        # 带一张可用墙卡 -> 垫背分支不触发，向日葵才能走正常经济路径
        b=self.pool([SUN,WALL],ps,zs,sun=650)
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==SUN]
        self.assertTrue(any(c.row==0 for c in cs), '有墙保护的高压路必须允许重建经济')
        self.assertFalse(any(c.row in (1,3,4,5) for c in cs), '危急路不补经济')

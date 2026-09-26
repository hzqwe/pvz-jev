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

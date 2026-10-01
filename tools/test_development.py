import unittest
import test_plant_knowledge as knowledge
from test_strategy import SUN, PEA, Plant, Zombie, JevAnswer, JevResponse
from pvz.policy import generate_candidates, merge_decision

class DevelopmentTests(unittest.TestCase):
    setUp = knowledge.PlantKnowledgeTests.setUp
    board = knowledge.PlantKnowledgeTests.board
    mature = knowledge.PlantKnowledgeTests.mature
    def hold(self,b):
        cs=generate_candidates(b,self.book)
        wait=next(c for c in cs if c.kind=='wait')
        resp=JevResponse(answers={'action':JevAnswer('action','choice',{'choice':wait.cid}),'hold_sun':JevAnswer('hold_sun','noul',{'noul':.99})})
        return merge_decision(resp,cs,b,self.book)

    def test_column_first_economy(self):
        b=self.board([SUN],plants=[Plant(0,0,0,SUN)])
        c=next(c for c in generate_candidates(b,self.book) if c.kind=='plant')
        self.assertEqual((c.row,c.col),(1,0))

    def test_quiet_rich_hold_completes_missing_defence(self):
        b=self.board([PEA],sun=1200)
        self.assertFalse(self.hold(b).hold)

    def test_weak_enemy_does_not_stop_empty_lane_prebuild(self):
        b=self.mature([PEA],[Zombie(0,0,0,x=720)],extra=[Plant(20,0,2,PEA)])
        self.assertTrue(any(c.kind=='plant' and c.row!=0 for c in generate_candidates(b,self.book)))

    def test_opening_economy_does_not_wait_for_zombies(self):
        self.assertFalse(self.hold(self.board([SUN],sun=400)).hold)

    def test_prebuild_keeps_reserve(self):
        self.assertTrue(self.hold(self.board([PEA],sun=150)).hold)

    def test_complete_lanes_can_wait(self):
        # 用户 2026-10-01：前四列填满仍须继续补阵；真正没有空位时可等。
        full=[Plant(r*9+i,r,i,SUN if i<2 else PEA) for r in range(5) for i in range(9)]
        b=self.board([PEA],plants=full,sun=1500)
        self.assertTrue(self.hold(b).hold)

    def test_opening_near_queen_price_reserves_then_buys(self):
        queen=knowledge.QUEEN
        cost=self.book.cost(queen)
        self.assertTrue(self.hold(self.board([SUN,queen],sun=cost-100)).hold)
        d=self.hold(self.board([SUN,queen],sun=cost))
        self.assertEqual(d.candidate.type_id,queen)
        self.assertIn(d.candidate.col,(1,2))

    def test_saving_queen_releases_for_danger(self):
        b=self.board([SUN,knowledge.QUEEN],[Zombie(0,0,0,x=110)],sun=400)
        self.assertFalse(self.hold(b).hold)

    def test_stall_estimate_requires_rear_fire_and_useful_delay(self):
        from pvz.tactics import stall_window
        b=self.board([SUN],[Zombie(0,0,0,x=240,hp=250)],[Plant(0,0,0,PEA)])
        self.assertIsNotNone(stall_window(b,self.book,0,SUN))
        b.zombies[0].hp=4000
        self.assertIsNone(stall_window(b,self.book,0,SUN))
        b.plants=[]
        self.assertIsNone(stall_window(b,self.book,0,SUN))

    def test_opening_queen_can_join_lanes_with_existing_shooters(self):
        queen=knowledge.QUEEN
        b=self.board([SUN,queen],plants=[Plant(r,r,2,PEA) for r in range(5)],sun=self.book.cost(queen))
        d=self.hold(b)
        self.assertFalse(d.hold)
        self.assertEqual((d.candidate.type_id,d.candidate.col),(queen,1))

    def test_stall_behind_shooter_does_not_claim_to_protect_it(self):
        from pvz.tactics import stall_window
        b=self.board([SUN],[Zombie(0,0,0,x=280,hp=30)],
                     [Plant(0,0,2,PEA),Plant(1,0,3,SUN)])
        self.assertIsNone(stall_window(b,self.book,0,SUN,1))
        self.assertFalse(any('speed bump' in c.why for c in generate_candidates(b,self.book)))

    def test_close_intercept_releases_early_economy_reserve(self):
        wall=knowledge.WALL
        b=self.board([wall],[Zombie(0,0,0,x=290)],[Plant(0,0,2,PEA)],sun=self.book.cost(wall))
        cs=[c for c in generate_candidates(b,self.book) if c.kind=='plant']
        self.assertTrue(cs)
        self.assertEqual(cs[0].col,3)

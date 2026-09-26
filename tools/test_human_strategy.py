"""User-confirmed costs, formation, and continuous recovery regressions."""
import unittest
import test_strategy as fixtures
from pvz.board import Plant,Zombie
from pvz.policy import generate_candidates,merge_decision
from pvz.jev import JevAnswer,JevResponse
QUEEN,SUN,PEA,RECLAIM,PAD=range(410,415)
class HumanStrategyTests(unittest.TestCase):
    board=fixtures.StrategyTests.board
    def setUp(self):
        fixtures.StrategyTests.setUp(self)
        for t,n in zip((QUEEN,SUN,PEA,RECLAIM,PAD),('向日葵女王','阳光向日葵','豌豆射手','回收高坚果','睡莲')):
            self.book.bind_one(t,n)
    def test_queen_dynamic_price_and_noise_rejection(self):
        self.book.sync_field_copies([]);self.assertEqual(self.book.cost(QUEEN),500)
        self.book.sync_field_copies([Plant(0,0,2,QUEEN)]);self.assertEqual(self.book.cost(QUEEN),600)
        self.book.set_real_cost(QUEEN,575)
        self.assertEqual(self.book.cost(QUEEN),600)
        self.book.sync_field_copies([]);self.assertEqual(self.book.cost(QUEEN),500)
    def test_queen_uses_third_column_before_empty_second(self):
        b=self.board([QUEEN],sun=500)
        cs=[c for c in generate_candidates(b,self.book) if c.type_id==QUEEN]
        self.assertTrue(cs);self.assertEqual(cs[0].col,2)
    def test_economy_after_queen_spends_with_200_sun(self):
        b=self.board([SUN],[Zombie(0,0,0,x=650,hp=270)],[Plant(0,0,2,QUEEN)],sun=200)
        cs=generate_candidates(b,self.book);w=next(c for c in cs if c.kind=='wait')
        r=JevResponse(answers={'action':JevAnswer('action','choice',{'choice':w.cid}),'hold_sun':JevAnswer('hold_sun','noul',{'noul':.99})})
        self.assertFalse(merge_decision(r,cs,b,self.book).hold)
    def test_rear_healthy_reclaim_wall_gets_relocation(self):
        b=self.board([RECLAIM],plants=[Plant(0,0,1,RECLAIM,hp=8000)],sun=500)
        cs=[c for c in generate_candidates(b,self.book) if c.kind=='shovel']
        self.assertTrue(cs)
        self.assertGreaterEqual(cs[0].relocate_to[1],4)
    def test_do_not_remove_only_wall_being_bitten_for_tidiness(self):
        b=self.board([RECLAIM],[Zombie(0,0,0,x=170,hp=2000)],[Plant(0,0,1,RECLAIM,hp=8000,recently_eaten=True)])
        self.assertFalse(any(c.kind=='shovel' for c in generate_candidates(b,self.book)))

    def test_later_economy_still_spends_small_budget(self):
        b=self.board([SUN],[Zombie(0,0,0,x=650,hp=270)],
            [Plant(0,0,2,QUEEN)]+[Plant(i+1,i+1,0,SUN) for i in range(4)],sun=200)
        cs=generate_candidates(b,self.book);w=next(c for c in cs if c.kind=='wait')
        r=JevResponse(answers={'action':JevAnswer('action','choice',{'choice':w.cid}),'hold_sun':JevAnswer('hold_sun','noul',{'noul':.99})})
        self.assertFalse(merge_decision(r,cs,b,self.book).hold)
    def test_fixed_cost_does_not_increase_with_copies(self):
        self.book.sync_field_copies([Plant(i,i,0,SUN) for i in range(5)])
        self.assertEqual(self.book.cost(SUN),100)
    def test_pool_wall_preparation_is_a_compound_rescue(self):
        b=self.board([PAD,RECLAIM],[Zombie(0,2,0,x=110,hp=1000)],sun=200)
        b.scene=2;b.rows=6
        cs=generate_candidates(b,self.book)
        self.assertTrue(any(c.type_id==PAD and c.supports_type==RECLAIM and c.emergency for c in cs))

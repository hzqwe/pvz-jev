"""Plant capabilities must affect decisions, not just decorative descriptions."""
import unittest
import test_strategy as fixtures
from test_strategy import SUN, PEA, WALL, STRONG
from pvz.board import Plant, Zombie
from pvz.tactics import saving_plan, lane_facts
from pvz.policy import generate_candidates
from pvz.serialize import build_state

QUEEN, CORN, GATLING, TALL, THUNDER = range(310,315)


class PlantKnowledgeTests(unittest.TestCase):
    board = fixtures.StrategyTests.board
    def setUp(self):
        fixtures.StrategyTests.setUp(self)
        for tid,name in zip(range(310,315),['向日葵女王','玉米卷香蒲','狂野机枪射手','高冰果','雷果子']):
            self.book.bind_one(tid,name)

    def mature(self,cards,zombies,sun=1200,extra=()):
        return self.board(cards,zombies,[Plant(r,r,0,SUN) for r in range(4)]+list(extra),sun)

    def test_queen_counts_as_actual_firepower(self):
        b=self.board([], [Zombie(0,0,0,x=650)], [Plant(0,4,1,QUEEN)])
        self.assertGreater(lane_facts(b,0,self.book)['shooter_support'],0)

    def test_multilane_saving_can_choose_ice_over_cheapest_corn(self):
        b=self.mature([CORN,STRONG],[Zombie(i,i,0,x=700) for i in range(4)],sun=400)
        self.assertEqual(saving_plan(b,self.book)['type_id'],STRONG)

    def test_concentrated_attack_values_gatling_above_global_corn(self):
        b=self.mature([CORN,GATLING],[Zombie(i,2,0,x=650+i*15) for i in range(4)],sun=450)
        self.assertEqual(saving_plan(b,self.book)['type_id'],GATLING)

    def test_armored_front_can_save_for_thunder(self):
        z=Zombie(0,2,2,x=650,hp=270,armor_hp=500)
        b=self.mature([THUNDER,CORN],[z],sun=350)
        self.assertEqual(saving_plan(b,self.book)['type_id'],THUNDER)

    def test_queen_still_offered_when_economy_slots_target_reached(self):
        ps=[Plant(r*2+c,r,c,SUN) for r in range(5) for c in range(2)]
        b=self.board([QUEEN],[Zombie(0,2,0,x=650)],ps,sun=1200)
        self.assertIn(QUEEN,{c.type_id for c in generate_candidates(b,self.book)})

    def test_all_available_types_get_representation_before_second_placement(self):
        from pvz.plants import load_lineups
        lineup=load_lineups()[0]
        tids=list(range(400,400+len(lineup['order'])))
        self.book.bind_lineup(tids,lineup['order'])
        b=self.mature(tids,[Zombie(i,4,0,x=150+i*12) for i in range(10)],sun=5000)
        offered={c.type_id for c in generate_candidates(b,self.book)}
        self.assertTrue(set(tids)-{t for t in tids if self.book.role(t)=='platform'} <= offered)

    def test_model_receives_damage_and_restrictions_for_seed_cards(self):
        b=self.board([THUNDER],[])
        info=build_state(b,self.book)['seed_cards'][0]
        self.assertIn('effect',info)
        self.assertIn('combat',info)
        self.assertEqual(info['combat']['armor_multiplier'],2)

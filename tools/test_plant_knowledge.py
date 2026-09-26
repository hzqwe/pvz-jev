"""Plant capabilities must affect decisions, not just decorative descriptions."""
import unittest
import test_strategy as fixtures
from test_strategy import SUN, PEA, WALL, STRONG, BOMB, FREEZE
from pvz.board import Plant, Zombie
from pvz.tactics import saving_plan, lane_facts, upgrade_value
from pvz.policy import generate_candidates
from pvz.serialize import build_state

QUEEN, CORN, GATLING, TALL, THUNDER = range(310,315)
CHILL = 315


class PlantKnowledgeTests(unittest.TestCase):
    board = fixtures.StrategyTests.board
    def setUp(self):
        fixtures.StrategyTests.setUp(self)
        for tid,name in zip(range(310,315),['向日葵女王','玉米卷香蒲','狂野机枪射手','高冰果','雷果子']):
            self.book.bind_one(tid,name)
        self.assertTrue(self.book.bind_one(CHILL,'寒光菇'))

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

    def test_burn_aura_only_credits_zombies_in_aura_reach(self):
        near=self.board([],[Zombie(0,2,0,x=300)],sun=0)
        far=self.board([],[Zombie(0,2,0,x=760)],sun=0)
        self.assertGreater(upgrade_value(near,self.book,QUEEN,2),
                           upgrade_value(far,self.book,QUEEN,2))

    def test_reflect_wall_in_contact_reduces_pressure(self):
        z=Zombie(0,0,0,x=250)          # 雷果子种在 col2 (x=240)，僵尸已啃到墙
        with_wall=self.board([],[z],[Plant(0,0,2,THUNDER)])
        without=self.board([],[z])
        fw=lane_facts(with_wall,0,self.book)
        self.assertGreater(fw['reflect_support'],0)
        self.assertLess(fw['pressure'],lane_facts(without,0,self.book)['pressure'])

    def test_reflect_wall_not_in_contact_counts_nothing(self):
        b=self.board([],[Zombie(0,0,0,x=700)],[Plant(0,0,2,THUNDER)])
        self.assertEqual(lane_facts(b,0,self.book)['reflect_support'],0)

    def test_death_freeze_wall_is_mentioned_to_the_model(self):
        b=self.board([TALL],[Zombie(0,0,0,x=300)])
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==TALL]
        self.assertTrue(cs)
        self.assertIn('death ripple',cs[0].why)

    def test_reclaimable_wall_is_mentioned_to_the_model(self):
        self.book.bind_one(320,'回收高坚果')
        b=self.board([320],[Zombie(0,0,0,x=300)])
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==320]
        self.assertTrue(cs)
        self.assertIn('Reclaimable wall',cs[0].why)

    def test_sun_on_kill_scales_with_victims(self):
        one=self.board([BOMB],[Zombie(0,2,0,x=240)])
        three=self.board([BOMB],[Zombie(0,2,0,x=240),Zombie(1,2,0,x=240),Zombie(2,2,0,x=240)])
        v1=max(c.score for c in generate_candidates(one,self.book)
               if c.kind=='plant' and c.type_id==BOMB)
        v3=max(c.score for c in generate_candidates(three,self.book)
               if c.kind=='plant' and c.type_id==BOMB)
        self.assertGreater(v3,v1)

    def test_chill_shroom_credits_sun_per_frozen(self):
        b=self.board([CHILL],[Zombie(i,i%5,0,x=500) for i in range(10)],sun=550)
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==CHILL]
        self.assertTrue(cs)
        self.assertIn('frozen zombie',cs[0].why)

"""Threat and action choices must use reachable targets and evidenced abilities."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.plants import PlantBook, KBEntry
from pvz.tactics import lane_facts, attack_dps
from pvz.policy import generate_candidates, merge_decision
from pvz.jev import JevAnswer, JevResponse
from pvz.serialize import build_state


class CombatProjectionTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.book.activate_catalog('classic', '3.9.9')
        for tid in (0, 9, 16, 86, 101, 183):
            self.book.bind_identity(tid)

    def board(self, zombies=(), plants=(), cards=(), sun=1200):
        return BoardState(ok=True, rows=5, cols=9, game_clock=20000, sun=sun,
                          zombies=list(zombies), plants=list(plants),
                          slots=[SeedSlot(i, tid, 0, 1000) for i, tid in enumerate(cards)])

    def test_out_of_range_shooter_cannot_reduce_lane_pressure(self):
        self.book.bind_one(400, '豌豆射手')
        raw = copy.deepcopy(self.book.kb_by_id[400].raw)
        raw['combat']['range_cells'] = 2
        self.book.kb_by_id[400] = KBEntry.from_json('short shooter', raw)
        b = self.board([Zombie(0,0,0,x=700)], [Plant(0,0,0,400)])
        self.assertEqual(lane_facts(b,0,self.book)['shooter_support'], 0)

    def test_passed_forward_shooter_does_not_defend_lead_enemy(self):
        b = self.board([Zombie(0,0,0,x=200), Zombie(1,0,0,x=650)], [Plant(0,0,4,183)])
        self.assertEqual(lane_facts(b,0,self.book)['shooter_support'], 0)

    def test_quiet_lane_retains_awake_forward_formation_power(self):
        b = self.board(plants=[Plant(0,0,2,183), Plant(1,0,1,0,asleep=True)])
        self.assertAlmostEqual(lane_facts(b,0,self.book)['shooter_support'],
                               attack_dps(self.book,183)/20, places=2)
        self.assertEqual(lane_facts(b,1,self.book)['shooter_support'],0)

    def test_lane_beam_credits_reachable_crowd_only(self):
        ps = [Plant(0,0,2,183)]
        one = self.board([Zombie(0,0,0,x=500)], ps)
        crowd = self.board([Zombie(i,0,0,x=500+i*40) for i in range(3)], ps)
        off_lane = self.board([Zombie(0,0,0,x=500), Zombie(1,1,0,x=550)], ps)
        support = lane_facts(one,0,self.book)['shooter_support']
        self.assertAlmostEqual(lane_facts(crowd,0,self.book)['shooter_support'], support*3)
        self.assertEqual(lane_facts(off_lane,0,self.book)['shooter_support'], support)

    def test_tracking_power_is_shared_not_multiplied_by_crowd(self):
        b = self.board([Zombie(i,i,0,x=600) for i in range(5)], [Plant(0,0,2,86)])
        support = sum(lane_facts(b,r,self.book)['shooter_support'] for r in range(5))
        self.assertAlmostEqual(support, attack_dps(self.book,86)/20, places=2)

    def test_torch_path_requires_awake_same_lane_between_shooter_and_target(self):
        from pvz.tactics import torch_path
        z = Zombie(0,0,0,x=600)
        for queen, expected in [(Plant(0,0,2,86),True),
                                (Plant(0,0,2,86,asleep=True),False),
                                (Plant(0,1,2,86),False),
                                (Plant(0,0,0,86),False),
                                (Plant(0,0,7,86),False)]:
            with self.subTest(queen=queen):
                self.assertEqual(torch_path(self.board([z],[queen]),self.book,0,0,1,z),expected)

    def test_unmeasured_torch_boost_not_used_in_guaranteed_damage(self):
        from pvz.tactics import target_dps
        z = Zombie(0,0,0,x=600)
        b = self.board([z],[Plant(0,0,2,86)])
        self.assertEqual(target_dps(b,self.book,0,0,1,z), attack_dps(self.book,0))

    def test_armored_target_gets_beam_armor_multiplier(self):
        from pvz.tactics import target_dps
        b = self.board()
        plain = Zombie(0,0,0,x=600,hp=270)
        armor = Zombie(1,0,0,x=600,hp=0,armor_hp=1000)
        self.assertEqual(target_dps(b,self.book,183,0,2,armor),
                         target_dps(b,self.book,183,0,2,plain)*2)
        armor.hp = 270
        self.assertLess(target_dps(b,self.book,183,0,2,armor),attack_dps(self.book,183)*2)

    def test_economy_summary_excludes_sleep_and_does_not_assume_growth(self):
        b = self.board(plants=[Plant(0,0,0,0),Plant(1,1,0,9),Plant(2,2,0,9,asleep=True)])
        economy = build_state(b,self.book)['economy']
        self.assertEqual(economy['sun_per_25s_estimate'],65)
        self.assertIn(0,economy['inferred_income_types'])

    def test_sleeping_torch_does_not_change_pea_placement(self):
        b = self.board([Zombie(0,0,0,x=700)], [Plant(0,0,3,86,asleep=True)], [0])
        peas = [c for c in generate_candidates(b,self.book) if c.type_id==0 and c.row==0]
        self.assertTrue(peas)
        self.assertEqual(peas[0].col,1)

    def test_surplus_with_weak_far_zombies_keeps_developing_formation(self):
        ps = [Plant(r*10+c,r,c,tid) for r in range(5)
              for c,tid in [(0,9),(1,0),(2,86),(4,101)]]
        b = self.board([Zombie(0,0,0,x=730,hp=270)],ps,[9,0])
        cs = generate_candidates(b,self.book)
        wait = next(c for c in cs if c.kind=='wait')
        resp = JevResponse(answers={'action':JevAnswer('action','choice',{'choice':wait.cid}),
                                   'hold_sun':JevAnswer('hold_sun','noul',{'noul':.99})})
        decision = merge_decision(resp,cs,b,self.book)
        self.assertFalse(decision.hold)
        self.assertEqual(decision.candidate.kind,'plant')
        self.assertGreaterEqual(b.sun-decision.candidate.total_cost(self.book),400)


if __name__=='__main__':
    unittest.main()

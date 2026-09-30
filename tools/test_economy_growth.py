"""Economy must grow through manageable waves, without overriding house rescue."""
import json
import unittest
from pathlib import Path

from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.jev import JevAnswer, JevResponse
from pvz.plants import PlantBook
from pvz.policy import generate_candidates, merge_decision, action_invalid_reason
from tools.replay_battle_review import snapshot


class EconomyGrowthTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.book.activate_catalog('classic', '3.9.9')
        for tid in (9, 14, 33, 34, 66, 86, 109, 161):
            self.book.bind_identity(tid)

    def board(self, cards=(9,), *, sun=100, plants=(), zombies=(), scene=0):
        return BoardState(ok=True, ui=3, scene=scene, rows=5, cols=9,
                          game_clock=10000, sun=sun, plants=list(plants),
                          zombies=list(zombies),
                          slots=[SeedSlot(i, tid, 0, 750) for i, tid in enumerate(cards)])

    def model_wait(self, board, *, hold=.16):
        cs = generate_candidates(board, self.book)
        wait = next(c for c in cs if c.kind == 'wait')
        response = JevResponse(answers={
            'action': JevAnswer('action', 'choice', {'choice': wait.cid, 'confidence': .8}),
            'hold_sun': JevAnswer('hold_sun', 'noul', {'noul': hold})})
        return merge_decision(response, cs, board, self.book)

    def test_recorded_economy_waits_become_safe_sunflower_purchases(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/economy_waits_20260930.json')
                           .read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(log=case['source_log'], line=case['line']):
                b = snapshot(case, self.book)
                for tid in {s.type_id for s in b.slots} | {p.type_id for p in b.plants}:
                    self.book.bind_identity(tid)
                d = self.model_wait(b, hold=case['model_hold']['noul'])
                self.assertFalse(d.hold)
                c = d.candidate
                self.assertEqual(c.supports_type if c.supports_type is not None else c.type_id,
                                 case['expected_producer'])
                self.assertFalse(c.intercept, 'Grow the economy; do not sacrifice this sunflower')
                self.assertIsNone(action_invalid_reason(c, b, self.book))

    def test_can_spend_last_sun_on_safe_first_producer(self):
        b = self.board(zombies=[Zombie(0, 4, 0, x=700, hp=270)])
        d = self.model_wait(b, hold=.99)
        self.assertEqual(d.candidate.type_id, 9)
        self.assertFalse(d.hold)

    def test_other_high_lane_does_not_block_safe_economy(self):
        b = self.board(sun=145, zombies=[Zombie(0, 4, 0, x=280, hp=270)])
        d = self.model_wait(b)
        self.assertEqual(d.candidate.type_id, 9)
        self.assertNotEqual(d.candidate.row, 4)

    def test_roof_growth_uses_complete_support_budget(self):
        b = self.board((9, 33), scene=4, sun=175)
        d = self.model_wait(b)
        self.assertEqual((d.candidate.type_id, d.candidate.supports_type), (33, 9))
        self.assertEqual(d.candidate.total_cost(self.book), 175)
        self.assertIsNone(action_invalid_reason(d.candidate, b, self.book))

    def test_underfunded_support_chain_does_not_spend(self):
        b = self.board((9, 33), scene=4, sun=174)
        self.assertTrue(self.model_wait(b).hold)

    def test_affordable_opening_defence_is_completed_before_economy(self):
        b = self.board((9, 34), sun=275, plants=[Plant(0, 1, 2, 34, hp=300)],
                       zombies=[Zombie(0, 4, 0, x=700, hp=270)])
        d = self.model_wait(b)
        self.assertEqual(d.candidate.type_id, 34)
        self.assertEqual(d.candidate.row, 3)

    def test_house_rescue_still_beats_growth(self):
        b = self.board((9, 161), sun=250, zombies=[Zombie(0, 4, 0, x=110, hp=270)])
        d = self.model_wait(b, hold=.99)
        self.assertEqual(d.candidate.type_id, 161)
        self.assertTrue(d.candidate.emergency)

    def test_cheap_sacrificial_interception_is_not_replaced_by_economy(self):
        b = self.board(sun=100, zombies=[Zombie(0, 4, 0, x=110, hp=270)])
        d = self.model_wait(b)
        self.assertEqual(d.candidate.row, 4)
        self.assertTrue(d.candidate.intercept)

    def test_cooling_sunflower_cannot_force_growth(self):
        b = self.board(sun=200, zombies=[Zombie(0, 4, 0, x=700, hp=270)])
        b.slots[0].cd_left=500
        self.assertTrue(self.model_wait(b).hold)

    def test_established_economy_does_not_force_repeated_sunflowers(self):
        b = self.board(sun=200,
                       plants=[Plant(r*2+c, r, c, 9, hp=300) for r in range(5) for c in range(2)],
                       zombies=[Zombie(0, 4, 0, x=700, hp=270)])
        self.assertTrue(self.model_wait(b).hold)


if __name__ == '__main__':
    unittest.main()

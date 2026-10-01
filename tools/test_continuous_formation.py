"""Surplus sun must build a balanced, supported formation through manageable waves."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.jev import JevAnswer, JevResponse
from pvz.plants import PlantBook
from pvz.policy import generate_candidates, merge_decision, action_invalid_reason
from tools.replay_battle_review import snapshot
from pvz.agent import PvZJevAgent


class ContinuousFormationTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.book.activate_catalog('classic', '3.9.9')
        for tid in (2, 9, 16, 33, 34, 39, 66, 86, 147, 161, 183, 189, 233):
            self.book.bind_identity(tid)

    def board(self, cards=(147,), plants=(), zombies=(), sun=1500, scene=0):
        return BoardState(ok=True, ui=3, rows=5, cols=9, scene=scene,
            game_clock=30000, sun=sun, plants=list(plants), zombies=list(zombies),
            slots=[SeedSlot(i, tid, 0, 1000) for i, tid in enumerate(cards)])

    def decide(self, b, prefer_row=None):
        cs = generate_candidates(b, self.book)
        pick = next((c for c in cs if c.kind == 'plant' and c.row == prefer_row),
                    next(c for c in cs if c.kind == 'wait'))
        response = JevResponse(answers={
            'action': JevAnswer('action', 'choice', {'choice': pick.cid, 'confidence': .9}),
            'hold_sun': JevAnswer('hold_sun', 'noul', {'noul': .99 if prefer_row is None else .1})})
        return merge_decision(response, cs, b, self.book)

    def test_latest_rich_waits_have_legal_permanent_construction(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/formation_waits_20261001.json')
                           .read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(line=case['line']):
                b = snapshot(case, self.book)
                for tid in {s.type_id for s in b.slots} | {p.type_id for p in b.plants}:
                    self.book.bind_identity(tid)
                d = self.decide(b)
                self.assertFalse(d.hold)
                c = d.candidate
                tid = c.supports_type or c.replacement_type or c.type_id
                self.assertFalse(self.book.has_tag(tid, 'instant'))
                self.assertFalse(self.book.has_tag(tid, 'temporary'))
                self.assertIsNone(action_invalid_reason(c, b, self.book))

    def test_rear_wall_does_not_hide_safe_shooter_cells_behind_front_wall(self):
        plants = [Plant(r, r, 0, 9, hp=300) for r in range(5)]
        plants += [Plant(10, 0, 1, 39, hp=4000), Plant(11, 0, 6, 161, hp=8000)]
        plants += [Plant(100+r*10+c, r, c, 147, hp=300)
                   for r in range(1,5) for c in (1,2,3)]
        b = self.board(plants=plants)
        cs = [c for c in generate_candidates(b, self.book) if c.kind == 'plant' and c.row == 0]
        self.assertTrue(any(1 < c.col < 6 for c in cs))
        self.assertTrue(all(action_invalid_reason(c, b, self.book) is None for c in cs))

    def test_four_filled_columns_are_not_a_completed_formation(self):
        plants = [Plant(r*10+c, r, c, 9 if c < 2 else 147, hp=300)
                  for r in range(5) for c in range(4)]
        d = self.decide(self.board(plants=plants))
        self.assertFalse(d.hold)
        self.assertGreaterEqual(d.candidate.col, 4)

    def test_one_distant_high_lane_does_not_freeze_other_safe_lanes(self):
        b = self.board(plants=[Plant(r, r, 0, 147, hp=300) for r in range(4)],
            zombies=[Zombie(0, 0, 0, x=850, hp=20000)])
        d = self.decide(b)
        self.assertFalse(d.hold)
        self.assertEqual(d.candidate.row, 4)

    def test_surplus_firepower_goes_to_weak_lane_even_if_all_lanes_have_shooters(self):
        plants = [Plant(r, r, 0, 147 if r < 4 else 34, hp=300) for r in range(5)]
        d = self.decide(self.board(plants=plants))
        self.assertFalse(d.hold)
        self.assertEqual(d.candidate.row, 4)

    def test_model_stacking_strong_lane_is_redirected_to_weak_lane(self):
        plants = [Plant(r, r, 0, 147 if r < 4 else 34, hp=300) for r in range(5)]
        d = self.decide(self.board(plants=plants), prefer_row=0)
        self.assertEqual(d.candidate.row, 4)

    def test_model_shielding_a_nearby_attacker_is_not_redirected(self):
        plants = [Plant(r, r, 3, 147 if r < 4 else 34, hp=300) for r in range(5)]
        b = self.board(cards=(161, 147), plants=plants,
                       zombies=[Zombie(0, 0, 0, x=430, hp=270)])
        cs = generate_candidates(b, self.book)
        wall = next(c for c in cs if c.kind == 'plant' and c.type_id == 161 and c.row == 0)
        response = JevResponse(answers={
            'action': JevAnswer('action', 'choice', {'choice': wall.cid, 'confidence': .9}),
            'hold_sun': JevAnswer('hold_sun', 'noul', {'noul': .1})})
        d = merge_decision(response, cs, b, self.book)
        self.assertEqual((d.candidate.type_id, d.candidate.row), (161, 0))

    def test_roof_expansion_includes_pot_and_attacker(self):
        plants = [Plant(r*10+c, r, c, 147, hp=300) for r in range(5) for c in range(3)]
        plants += [Plant(100+r*10+c, r, c, 66, hp=300) for r in range(5) for c in range(3)]
        d = self.decide(self.board(cards=(33, 147), plants=plants, scene=4))
        self.assertFalse(d.hold)
        self.assertEqual((d.candidate.type_id, d.candidate.supports_type), (33, 147))
        self.assertEqual(d.candidate.total_cost(self.book), 575)

    def test_quiet_front_wall_keeps_moving_formation_forward(self):
        plants = [Plant(r*10, r, 0, 147, hp=300) for r in range(5)]
        plants += [Plant(r*10+1, r, 4, 161, hp=8000) for r in range(5)]
        d = self.decide(self.board(cards=(161,), plants=plants))
        self.assertFalse(d.hold)
        self.assertGreater(d.candidate.col, 4)

    def test_two_old_walls_do_not_stop_a_full_rear_formation_expanding(self):
        plants = [Plant(r*10+c, r, c, 147 if c < 4 else 161, hp=8000)
                  for r in range(5) for c in range(6)]
        d = self.decide(self.board(cards=(161,), plants=plants))
        self.assertFalse(d.hold)
        self.assertEqual(d.candidate.col, 7)  # Leave column G for the next attacker.
        expanded = plants + [Plant(100, d.candidate.row, 7, 161, hp=8000)]
        attack = self.decide(self.board(cards=(147,), plants=expanded))
        self.assertEqual((attack.candidate.row, attack.candidate.col), (d.candidate.row, 6))

    def test_construction_keeps_reserve_and_cooling_cards_cannot_be_used(self):
        self.assertTrue(self.decide(self.board(sun=650)).hold)
        b = self.board()
        b.slots[0].cd_left = 500
        self.assertTrue(self.decide(b).hold)

    def test_immediate_house_rescue_still_beats_quiet_construction(self):
        b = self.board(cards=(2, 147), zombies=[Zombie(0, 0, 0, x=110, hp=270)])
        d = self.decide(b)
        self.assertEqual(d.candidate.type_id, 2)
        self.assertEqual(d.candidate.row, 0)

    def test_early_income_fills_the_first_column_before_second_column(self):
        b = self.board(cards=(9,), sun=400,
                       plants=[Plant(0,0,0,9,hp=300),Plant(1,1,1,9,hp=300)] +
                              [Plant(10+r*2+c,r,c,9,hp=300) for r in range(2,5) for c in (0,1)])
        d = self.decide(b)
        self.assertEqual(d.candidate.col, 0)

    def test_rapid_fill_uses_the_same_weak_lane_investment(self):
        plants = [Plant(r,r,0,9,hp=300) for r in range(5)]
        plants += [Plant(10+r,r,2,147 if r < 4 else 34,hp=300) for r in range(5)]
        b = self.board(cards=(9,147,161),plants=plants)
        a = object.__new__(PvZJevAgent)
        a.book = self.book; a._frozen_hits = 0; a._advances = 10
        a.reader = SimpleNamespace(read=lambda:b)
        a.runtime_metadata = lambda:{}
        a.log = SimpleNamespace(append=lambda record:None)
        picks = []
        def execute(d, record, board):
            picks.append(d.candidate)
            record['executed'] = {'placed':True}
        a.execute = execute
        a.rapid_fill(b,count=1)
        self.assertEqual(len(picks), 1)
        self.assertEqual((picks[0].type_id,picks[0].row), (147,4))


if __name__ == '__main__':
    unittest.main()

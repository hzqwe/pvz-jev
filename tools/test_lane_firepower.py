"""Weak-lane repairs must beat income spam without blind destructive shoveling."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.jev import JevAnswer, JevResponse
from pvz.plants import PlantBook
from pvz.policy import generate_candidates, merge_decision, action_invalid_reason, burst_targets, escalate_emergency
from pvz.tactics import lane_facts
from pvz.agent import PvZJevAgent
from pvz.ui import Layout
from pvz.transactions import run_transaction
from tools import test_transactions as transaction_tests
from tools.replay_battle_review import snapshot


class LaneFirepowerTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.book.activate_catalog('classic', '3.9.9')
        for tid in (2, 9, 33, 34, 66, 147, 161, 183, 233):
            self.book.bind_identity(tid)

    def board(self, *, sun=600, cards=(9, 147), plants=None, zombies=None):
        return BoardState(ok=True, ui=3, scene=0, rows=5, cols=9,
            game_clock=12000, sun=sun,
            plants=plants if plants is not None else
                [Plant(r, r, 0, 9, hp=300) for r in range(5)],
            zombies=zombies if zombies is not None else [Zombie(0, 0, 4, x=480, hp=270, armor_hp=1100)],
            slots=[SeedSlot(i, tid, 0, 1000) for i, tid in enumerate(cards)])

    def decide(self, board, prefer_type=None):
        cs = generate_candidates(board, self.book)
        chosen = next((c for c in cs if c.kind == 'plant' and c.type_id == prefer_type),
                      next(c for c in cs if c.kind == 'wait'))
        resp = JevResponse(answers={
            'action': JevAnswer('action', 'choice', {'choice': chosen.cid, 'confidence': .9}),
            'hold_sun': JevAnswer('hold_sun', 'noul', {'noul': .1})})
        return merge_decision(resp, cs, board, self.book)

    def test_walls_do_not_count_as_sustained_firepower(self):
        b = self.board(plants=[Plant(0, 0, 0, 9, hp=300),
                               Plant(1, 0, 1, 161, hp=8000),
                               Plant(2, 0, 2, 233, hp=4000)])
        f = lane_facts(b, 0, self.book)
        self.assertEqual(f.get('lead_dps_estimate'), 0)
        self.assertTrue(f.get('needs_firepower'))

    def test_cross_lane_fire_counts_only_when_it_can_reach_lead(self):
        b = self.board(plants=[Plant(0, 1, 2, 34, hp=300)],
                       zombies=[Zombie(0, 0, 0, x=500, hp=270)])
        self.assertEqual(lane_facts(b, 0, self.book).get('lead_dps_estimate'), 10)
        self.assertFalse(lane_facts(b, 0, self.book).get('needs_firepower'))
        b.zombies[0].x = 200
        self.assertEqual(lane_facts(b, 0, self.book).get('lead_dps_estimate'), 0)

    def test_ready_premium_attack_beats_another_sunflower_on_pressured_lane(self):
        b = self.board()
        d = self.decide(b, prefer_type=9)
        self.assertEqual(d.candidate.type_id, 147)
        self.assertEqual(d.candidate.row, 0)

    def test_income_does_not_reset_near_term_firepower_savings(self):
        b = self.board(sun=400)
        self.assertFalse(any(c.kind == 'plant' and c.type_id == 9
                             and not c.intercept for c in generate_candidates(b, self.book)))
        self.assertTrue(self.decide(b).hold)

    def test_nearest_weak_lane_repair_survives_per_card_shortlist_limit(self):
        b = self.board(zombies=[Zombie(0, 0, 0, x=400, hp=270),
                               Zombie(1, 2, 0, x=480, hp=5000),
                               Zombie(2, 4, 0, x=500, hp=5000)])
        d = self.decide(b, prefer_type=9)
        self.assertEqual(d.candidate.row, 0)
        self.assertEqual(d.candidate.type_id, 147)

    def test_early_economy_is_not_disabled_by_far_ordinary_zombie(self):
        b = self.board(sun=100, plants=[], zombies=[Zombie(0, 0, 0, x=800, hp=270)])
        self.assertEqual(self.decide(b).candidate.type_id, 9)

    def test_recorded_wealthy_weak_lane_gets_safe_premium_replacement(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/lane_firepower_20260930.json')
                           .read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(line=case['line']):
                b = snapshot(case, self.book)
                for tid in {s.type_id for s in b.slots} | {p.type_id for p in b.plants}:
                    self.book.bind_identity(tid)
                repairs = [c for c in generate_candidates(b, self.book)
                           if c.kind == 'shovel' and c.row == 0
                           and c.replacement_intent == 'combat']
                self.assertTrue(repairs)
                c = repairs[0]
                self.assertEqual(c.type_id, 9)
                self.assertTrue(self.book.has_tag(c.replacement_type, 'shooter'))
                self.assertGreaterEqual(self.book.cost(c.replacement_type), 300)
                self.assertIsNone(action_invalid_reason(c, b, self.book))
                self.assertEqual(self.decide(b).candidate.row, 0)

    def test_cooling_or_unaffordable_upgrade_never_shovels(self):
        b = self.board(sun=499, plants=[Plant(r, r, 0, 9, hp=300) for r in range(5)] +
                       [Plant(8, 0, 1, 161, hp=8000)])
        self.assertFalse(any(c.replacement_intent == 'combat' for c in generate_candidates(b, self.book)))
        b.sun = 600; b.slots[1].cd_left = 500
        self.assertFalse(any(c.replacement_intent == 'combat' for c in generate_candidates(b, self.book)))

    def test_frontline_emergency_never_shovels_income_to_upgrade(self):
        b = self.board(zombies=[Zombie(0, 0, 0, x=110, hp=270)],
                       plants=[Plant(r, r, 0, 9, hp=300) for r in range(5)] +
                       [Plant(8, 0, 1, 161, hp=8000)])
        self.assertFalse(any(c.replacement_intent == 'combat' for c in generate_candidates(b, self.book)))

    def test_burst_addresses_nearest_house_lane_before_larger_remote_cluster(self):
        b = self.board(cards=(2,), sun=600, plants=[],
            zombies=[Zombie(0, 0, 0, x=110, hp=270)] +
                    [Zombie(i+1, 4, 0, x=145+i, hp=270) for i in range(8)])
        d = self.decide(b)
        self.assertIn(b.zombies[0], burst_targets(d.candidate, b, self.book))

    def test_near_pressured_lane_burst_beats_premium_repair(self):
        b = self.board(cards=(9, 2, 147),
                       zombies=[Zombie(0, 0, 4, x=300, hp=270, armor_hp=900)])
        d = self.decide(b, prefer_type=9)
        self.assertEqual(d.candidate.type_id, 2)
        self.assertIn(b.zombies[0], burst_targets(d.candidate, b, self.book))

    def test_remote_wall_recovery_cannot_interrupt_nearby_effective_ash(self):
        b = self.board(cards=(9, 2, 147),
                       plants=[Plant(r, r, 0, 9, hp=300) for r in range(5)] +
                              [Plant(8, 4, 4, 161, hp=8000)],
                       zombies=[Zombie(0, 0, 4, x=300, hp=270, armor_hp=900),
                                Zombie(1, 4, 5, x=500, hp=1000)])
        d = self.decide(b, prefer_type=9)
        self.assertEqual(d.candidate.type_id, 2)
        self.assertIsNone(escalate_emergency(d.candidate, b, self.book))
        self.assertEqual(escalate_emergency(None, b, self.book).type_id, 2)

    def test_execution_reprioritizes_a_new_closer_house_threat(self):
        b = self.board(cards=(2,), plants=[],
            zombies=[Zombie(0, 0, 0, x=110, hp=270), Zombie(1, 4, 0, x=145, hp=270)])
        pending = next(c for c in generate_candidates(b, self.book)
                       if c.emergency and b.zombies[1] in burst_targets(c, b, self.book))
        rescue = escalate_emergency(pending, b, self.book)
        self.assertIsNotNone(rescue)
        self.assertIn(b.zombies[0], burst_targets(rescue, b, self.book))

    def test_quiet_roof_pot_candidates_obey_the_same_rear_safety_as_execution(self):
        b = self.board(sun=700, cards=(33,), zombies=[],
                       plants=[Plant(r, r, 0, 9, hp=300) for r in range(5)] +
                              [Plant(8, 0, 1, 161, hp=8000)])
        b.scene = 4
        cs = [c for c in generate_candidates(b, self.book) if c.kind == 'plant']
        self.assertTrue(cs)
        for c in cs:
            self.assertIsNone(action_invalid_reason(c, b, self.book), c)

    def test_rapid_income_fill_cannot_bypass_weak_lane_priority(self):
        b = self.board(sun=1000, cards=(9, 147, 161))
        a = object.__new__(PvZJevAgent)
        a.book = self.book; a._frozen_hits = 0; a._advances = 10
        a.reader = SimpleNamespace(read=lambda: b)
        a.runtime_metadata = lambda: {}
        a.log = SimpleNamespace(append=lambda record: None)
        with patch.object(a, 'execute') as execute:
            a.rapid_fill(b)
        execute.assert_not_called()

    def test_premium_replacement_preserves_roof_pot_and_rechecks_danger(self):
        case = json.loads((Path(__file__).parent / 'fixtures/lane_firepower_20260930.json')
                          .read_text(encoding='utf-8'))[1]
        for fault in (None, 'cooling', 'too_close'):
            with self.subTest(fault=fault):
                b = snapshot(case, self.book)
                for tid in {s.type_id for s in b.slots} | {p.type_id for p in b.plants}:
                    self.book.bind_identity(tid)
                c = next(c for c in generate_candidates(b, self.book)
                         if c.replacement_intent == 'combat' and c.kind == 'shovel' and c.row == 0)
                book = self.book
                class UpgradeGame(transaction_tests.FakeGame):
                    def shovel_hotkey(self):
                        super().shovel_hotkey()
                        if fault == 'cooling':
                            next(s for s in self.state.slots if s.type_id == c.replacement_type).cd_left = 100
                        if fault == 'too_close':
                            self.state.zombies = [Zombie(0, c.row, 0, x=80+80*c.col+100, hp=270)]
                    def click_card(self, index, *args):
                        super().click_card(index, *args)
                        self.state.held_type = next(s.type_id for s in self.state.slots if s.index == index)
                    def click_grid(self, row, col, *args, dx=0):
                        if self.state.held_cursor == 6:
                            self.actions.append('shovel')
                            self.state.plants = [p for p in self.state.plants if p.index != c.source_index]
                        else:
                            self.actions.append('plant')
                            tid = self.state.held_type
                            self.state.plants.append(Plant(999, row, col, tid, hp=1000))
                            self.state.sun -= book.cost(tid)
                        self.cancel_seed()
                g = UpgradeGame(); g.state = b
                a = SimpleNamespace(reader=g, clicker=g, book=book, layout=Layout(), _geometry_error=None)
                with patch('pvz.transactions.time.sleep'):
                    result = run_transaction(a, g.read(), candidate=c)
                self.assertEqual(result['completed'], fault is None, result)
                if fault:
                    self.assertNotIn('shovel', g.actions)
                else:
                    self.assertTrue(any(p.cell == (c.row, c.col) and p.type_id == 66 for p in g.state.plants))
                    self.assertTrue(any(p.cell == (c.row, c.col) and p.type_id == c.replacement_type for p in g.state.plants))


if __name__ == '__main__':
    unittest.main()

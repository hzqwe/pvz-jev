"""Regress the 22:45 roof battle without model calls or real game input."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pvz.board import Plant, SeedSlot, Zombie
from pvz.jev import JevAnswer, JevResponse
from pvz.policy import Candidate, generate_candidates, merge_decision, burst_targets
from pvz.transactions import PlantTransaction, TransactionStopped, run_transaction
from tools.replay_battle_review import snapshot
from tools import test_economy_growth as economy_tests
from tools import test_transactions as transaction_tests

FakeGame = transaction_tests.FakeGame


def recorded_cases():
    return json.loads((Path(__file__).parent / 'fixtures/roof_followup_20260930.json')
                      .read_text(encoding='utf-8'))


class EconomyPriorityTests(unittest.TestCase):
    def setUp(self):
        self.helper = economy_tests.EconomyGrowthTests()
        self.helper.setUp()
        self.book = self.helper.book

    def board(self, *args, **kwargs):
        return self.helper.board(*args, **kwargs)

    def choose_wall(self, board):
        cs = generate_candidates(board, self.book)
        wall = next(c for c in cs if c.kind == 'plant' and
                    self.book.has_tag(c.supports_type if c.supports_type is not None
                                      else c.type_id, 'wall'))
        resp = JevResponse(answers={
            'action': JevAnswer('action', 'choice', {'choice': wall.cid, 'confidence': .9}),
            'hold_sun': JevAnswer('hold_sun', 'noul', {'noul': .1})})
        return wall, merge_decision(resp, cs, board, self.book)

    def test_recorded_far_wall_does_not_starve_two_producer_economy(self):
        case = recorded_cases()[0]
        b = snapshot(case, self.book)
        for tid in {s.type_id for s in b.slots} | {p.type_id for p in b.plants}:
            self.book.bind_identity(tid)
        wall, d = self.choose_wall(b)
        self.assertEqual(wall.supports_type, 233)
        self.assertEqual(d.candidate.type_id, 9)
        self.assertFalse(d.candidate.intercept)

    def test_near_wall_retains_priority_over_growth(self):
        b = self.board((9, 161), sun=250,
                       zombies=[Zombie(0, 4, 0, x=300, hp=270)])
        wall, d = self.choose_wall(b)
        self.assertEqual(d.candidate, wall)

    def test_four_producers_allow_proactive_wall_building(self):
        b = self.board((9, 161), sun=250,
                       plants=[Plant(r, r, 0, 9, hp=300) for r in range(4)],
                       zombies=[Zombie(0, 4, 0, x=700, hp=270)])
        wall, d = self.choose_wall(b)
        self.assertEqual(d.candidate, wall)

    def test_expensive_front_asset_can_be_protected_before_growth(self):
        self.book.bind_identity(86)
        b = self.board((9, 161), sun=250, plants=[Plant(0, 4, 3, 86, hp=1000)],
                       zombies=[Zombie(0, 4, 0, x=700, hp=270)])
        wall, d = self.choose_wall(b)
        self.assertEqual(d.candidate, wall)


class BlastFollowupTests(unittest.TestCase):
    def test_recorded_blasts_remain_legal_after_lead_passes_destination(self):
        from pvz.plants import PlantBook
        for case in recorded_cases()[1:]:
            with self.subTest(line=case['line']):
                book = PlantBook(hybrid_file='', cost_file='', ids_file='')
                book.activate_catalog('classic', '3.9.9')
                b = snapshot({'board': case['followup_board']}, book)
                for tid in {s.type_id for s in b.slots} | {p.type_id for p in b.plants}:
                    book.bind_identity(tid)
                c = Candidate(**case['chosen'])
                self.assertTrue(burst_targets(c, b, book))
                agent = SimpleNamespace(book=book, layout=transaction_tests.TransactionTests().agent(FakeGame()).layout)
                PlantTransaction(agent, b).cell_valid(c.supports_type, c.row, c.col)

    def test_roof_pot_then_bomb_completes_behind_lead_enemy(self):
        class BombGame(FakeGame):
            def click_card(self, index, *args):
                super().click_card(index, *args)
                self.state.held_type = self.state.slots[index].type_id
            def click_grid(self, row, col, *args, dx=0):
                tid = self.state.held_type
                super().click_grid(row, col, *args, dx=dx)
                self.state.sun -= 200 if tid == 2 else 0
        g = BombGame()
        g.state.scene = 4; g.state.rows = 5; g.state.sun = 320
        g.state.plants = [Plant(0, 0, 2, 33, hp=300)]
        g.state.slots = [SeedSlot(0, 2, 0, 5000)]
        g.state.zombies = [Zombie(0, 0, 0, x=150, hp=270)]
        a = transaction_tests.TransactionTests().agent(g)
        a.book.activate_catalog('classic', '3.9.9')
        for tid in (2, 33): a.book.bind_identity(tid)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), followup=(2, 0, 2))
        self.assertTrue(result['completed'], result)
        self.assertTrue(any(p.type_id == 2 and p.cell == (0, 2) for p in g.state.plants))
        self.assertEqual(g.state.sun, 120)

    def test_wall_behind_passed_enemy_still_rejected(self):
        g = FakeGame(); g.state.zombies = [Zombie(0, 0, 0, x=100, hp=270)]
        with self.assertRaisesRegex(TransactionStopped, 'passed'):
            PlantTransaction(transaction_tests.TransactionTests().agent(g), g.read()).cell_valid(161, 0, 4)

    def test_blast_out_of_range_does_not_spend(self):
        g = FakeGame(); g.state.scene = 4; g.state.rows = 5
        g.state.plants = [Plant(0, 0, 6, 33, hp=300)]
        g.state.sun = 400; g.state.slots = [SeedSlot(0, 2, 0, 5000)]
        g.state.zombies = [Zombie(0, 0, 0, x=100, hp=270)]
        a = transaction_tests.TransactionTests().agent(g)
        a.book.activate_catalog('classic', '3.9.9')
        for tid in (2, 33): a.book.bind_identity(tid)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), followup=(2, 0, 6))
        self.assertFalse(result['completed'])
        self.assertNotIn('pick_pad', g.actions)
        self.assertEqual(g.state.sun, 400)


if __name__ == '__main__':
    unittest.main()

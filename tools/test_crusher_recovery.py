"""Crusher recovery regressions; simulated boards only, no game or Jev input."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.plants import PlantBook
from pvz.policy import Candidate, Decision, generate_candidates, merge_decision, escalate_emergency, action_invalid_reason
from pvz.agent import PvZJevAgent, AgentConfig, AgentStats
from pvz.jev import JevAnswer, JevResponse
from pvz.transactions import run_transaction
import test_transactions as fixtures
import review_battle

WALL, PAD, BOMB = 161, 16, 400


class CrusherRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        for tid, name in ((WALL, '回收高坚果'), (PAD, '睡莲'), (BOMB, '阳光炸弹')):
            self.book.bind_one(tid, name)

    def board(self, hp=8000, truck_x=520):
        return BoardState(ok=True, ui=3, rows=5, cols=9, sun=1000, game_clock=1000,
                          plants=[Plant(1, 0, 4, WALL, hp=hp)],
                          zombies=[Zombie(1, 0, 5, x=truck_x, hp=1000)])

    def rescue(self, b):
        return next((c for c in generate_candidates(b, self.book)
                     if c.kind == 'shovel' and c.type_id == WALL), None)

    def test_full_health_wall_is_recovered_before_crusher(self):
        b = self.board()
        c = self.rescue(b)
        self.assertIsNotNone(c)
        self.assertTrue(c.crush_recovery)
        self.assertFalse(c.emergency, 'Asset recovery must not claim to save the house')
        self.assertNotEqual(c.relocate_to[0], 0)
        self.assertIsNone(action_invalid_reason(c, b, self.book))

    def test_normal_zombie_in_front_does_not_hide_truck(self):
        b = self.board()
        b.zombies.append(Zombie(2, 0, 0, x=420))
        self.assertIsNotNone(self.rescue(b))

    def test_far_truck_and_nonviable_wall_are_not_shoveled(self):
        for hp, x in ((8000, 650), (8000, 320), (800, 520), (None, 520)):
            with self.subTest(hp=hp, x=x):
                self.assertIsNone(self.rescue(self.board(hp, x)))

    def test_ordinary_or_friendly_zombie_does_not_trigger_crush_recovery(self):
        for kind, friendly in ((0, False), (5, True), (9999, False)):
            b = self.board()
            b.zombies[0].type_id = kind
            b.zombies[0].friendly = friendly
            self.assertIsNone(self.rescue(b))

    def test_model_wait_and_execution_wait_are_preempted(self):
        b = self.board()
        cs = generate_candidates(b, self.book)
        wait = next(c for c in cs if c.kind == 'wait')
        resp = JevResponse(answers={
            'action': JevAnswer('action', 'choice', {'choice': wait.cid, 'confidence': 1}),
            'hold_sun': JevAnswer('hold_sun', 'noul', {'noul': .99})})
        self.assertEqual(merge_decision(resp, cs, b, self.book).candidate.kind, 'shovel')
        self.assertEqual(escalate_emergency(wait, b, self.book).kind, 'shovel')

    def test_execute_refreshes_wait_when_truck_arrives_during_inference(self):
        old = self.board(truck_x=650)
        fresh = self.board()
        fresh.game_clock = old.game_clock + 100
        a = PvZJevAgent.__new__(PvZJevAgent)
        a.book = self.book
        a.cfg = AgentConfig(dry_run=False, verbose=False)
        a.stats = AgentStats()
        a._frozen_hits = 0
        a._advances = 3
        a._action_times = []
        a.reader = SimpleNamespace(read=lambda: fresh)
        a.clicker = Mock()
        a.layout = Mock()
        a.layout.configure_board.side_effect = RuntimeError('stop before input')
        d = Decision(candidate=Candidate('WAIT', 'wait'), hold=True)
        record = {'decision': {'hold': True}}
        with self.assertRaisesRegex(RuntimeError, 'stop before input'):
            a.execute(d, record, old)
        self.assertTrue(d.candidate.crush_recovery)
        self.assertFalse(d.hold)
        self.assertIn('crusher', ' '.join(d.notes))
        self.assertEqual(a.clicker.mock_calls, [])

    def test_report_separates_parked_card_from_confirmed_planting(self):
        records = [{'executed': {'kind': 'recovered_for_later', 'completed': True,
                    'steps': [{'step': 'crush_recovery_started'},
                              {'step': 'source_removed', 'cell': [0, 4]},
                              {'step': 'recovered_card_parked', 'drop_index': 1}]}}]
        k = review_battle.kpis(records)
        self.assertEqual(k['crusher_recovery_attempts'], 1)
        self.assertEqual(k['parked_recovery_cards'], 1)
        self.assertEqual(k['transaction_placements'], 0)
        self.assertEqual(k['shovels'], 1)

    def test_house_rescue_has_priority_over_recovering_asset(self):
        b = self.board()
        b.zombies.append(Zombie(2, 4, 0, x=100, hp=1000))
        b.slots = [SeedSlot(0, BOMB, 0, 1000)]
        d = merge_decision(None, generate_candidates(b, self.book), b, self.book)
        self.assertTrue(d.candidate.emergency)
        self.assertEqual(d.candidate.kind, 'plant')

    def test_no_safe_destination_still_recovers_card(self):
        b = self.board()
        b.zombies = [Zombie(r, r, 5, x=520) for r in range(5)]
        c = self.rescue(b)
        self.assertIsNotNone(c)
        self.assertIsNone(c.relocate_to)
        self.assertIsNone(action_invalid_reason(c, b, self.book))
        self.assertIn('later', c.describe(self.book))

    def game(self):
        g = fixtures.FakeGame()
        g.state.plants = [Plant(1, 0, 4, WALL, hp=8000)]
        g.state.zombies = [Zombie(1, 0, 5, x=520, hp=1000),
                           Zombie(2, 2, 0, x=650, hp=1000)]
        return g, fixtures.TransactionTests().agent(g)

    def test_recovery_shovels_before_any_pad_purchase(self):
        g, a = self.game()
        c = self.rescue(g.state)
        self.assertIsNotNone(c)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), candidate=c)
        self.assertTrue(result['completed'], result)
        self.assertNotIn('place_pad', g.actions)
        self.assertEqual(g.state.sun, 100)
        wall = next(p for p in g.state.plants if p.type_id == WALL)
        self.assertNotEqual(wall.row, 0)
        self.assertEqual(wall.hp, 7200)
        self.assertLess(g.actions.index('shovel'), g.actions.index('pick_drop'))
        self.assertLess(g.actions.index('pick_drop'), g.actions.index('place_wall'))

    def test_existing_water_pad_allows_continuous_replant_without_purchase(self):
        g, a = self.game()
        g.state.plants.append(Plant(2, 2, 4, PAD))
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), candidate=self.rescue(g.state))
        self.assertTrue(result['completed'], result)
        self.assertNotIn('pick_pad', g.actions)
        self.assertEqual(next(p for p in g.state.plants if p.type_id == WALL).cell, (2, 4))

    def test_paused_game_does_not_issue_urgent_shovel(self):
        g, a = self.game()
        c = self.rescue(g.state)
        g.frozen = True
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), candidate=c)
        self.assertFalse(result['completed'])
        self.assertNotIn('key1', g.actions)

    def test_exact_health_boundary_and_cleared_threat_revalidation(self):
        b = self.board(hp=801)
        c = self.rescue(b)
        self.assertIsNotNone(c)
        b.plants[0].hp = 800
        self.assertEqual(action_invalid_reason(c, b, self.book), 'recovery health window closed')
        b.plants[0].hp = 8000
        b.zombies = []
        self.assertEqual(action_invalid_reason(c, b, self.book), 'crusher no longer approaching source')

    def test_urgent_source_health_window_closed_at_hotkey_keeps_source(self):
        class LowHealthGame(fixtures.FakeGame):
            def shovel_hotkey(self):
                super().shovel_hotkey()
                self.state.plants[0].hp = 800
        g = LowHealthGame()
        g.state.plants = [Plant(1, 0, 4, WALL, hp=8000)]
        g.state.zombies = [Zombie(1, 0, 5, x=520)]
        a = fixtures.TransactionTests().agent(g)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), candidate=self.rescue(g.state))
        self.assertFalse(result['completed'])
        self.assertNotIn('shovel', g.actions)
        self.assertEqual(len(g.state.plants), 1)

    def test_card_can_be_left_verified_on_lawn_then_reused(self):
        g, a = self.game()
        g.state.zombies = [Zombie(r, r, 5, x=520) for r in range(6)]
        c = self.rescue(g.state)
        self.assertIsNotNone(c)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), candidate=c)
        self.assertTrue(result['completed'], result)
        self.assertEqual(result['kind'], 'recovered_for_later')
        self.assertNotIn('pick_drop', g.actions)
        self.assertNotIn('place_wall', g.actions)
        self.assertEqual(len(g.state.dropped_seeds), 1)
        g.state.zombies = []
        pick = next(c for c in generate_candidates(g.state, a.book) if c.kind == 'pick')
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), pick=pick)
        self.assertTrue(result['completed'], result)
        self.assertEqual(next(p for p in g.state.plants if p.type_id == WALL).hp, 7200)

    def test_new_truck_at_destination_during_pickup_retargets_card(self):
        class MovingGame(fixtures.FakeGame):
            def click_client(self, *args):
                super().click_client(*args)
                self.state.zombies.append(Zombie(3, 1, 5, x=520))
        g = MovingGame()
        g.state.plants = [Plant(1, 0, 4, WALL, hp=8000)]
        g.state.zombies = [Zombie(1, 0, 5, x=520)]
        a = fixtures.TransactionTests().agent(g)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), candidate=self.rescue(g.state))
        self.assertTrue(result['completed'], result)
        self.assertNotIn(next(p for p in g.state.plants if p.type_id == WALL).row, (0, 1))

    def test_source_destroyed_before_shovel_is_not_reported_as_recovered(self):
        class LostGame(fixtures.FakeGame):
            def shovel_hotkey(self):
                super().shovel_hotkey()
                self.state.plants = []
        g = LostGame()
        g.state.plants = [Plant(1, 0, 4, WALL, hp=8000)]
        g.state.zombies = [Zombie(1, 0, 5, x=520)]
        a = fixtures.TransactionTests().agent(g)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(a, g.read(), candidate=self.rescue(g.state))
        self.assertFalse(result['completed'])
        self.assertNotIn('shovel', g.actions)


if __name__ == '__main__':
    unittest.main()

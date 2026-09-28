"""A cell-targeted shovel must never remove a different plant layer."""
import unittest
from unittest.mock import patch
from tools.test_ability_terrain import UpgradeGame
from pvz.board import Zombie
from pvz.policy import Candidate, action_invalid_reason, generate_candidates
from pvz.transactions import run_transaction


class StackSafetyTests(unittest.TestCase):
    def game(self, fault=None):
        game = UpgradeGame(fault)
        game.state.plants[0].hp = 300
        game.state.plants[1].hp = 1000
        game.state.zombies = [Zombie(0, 2, 0, x=200, hp=270)]
        return game

    def test_salvage_does_not_target_platform_below_healthy_body(self):
        game = self.game()
        candidates = generate_candidates(game.state, game.book)
        self.assertFalse(any(c.kind=='shovel' and c.type_id==16 for c in candidates))
        stale = Candidate('old', 'shovel', 2, 1, type_id=16, salvage=True)
        self.assertIsNotNone(action_invalid_reason(stale, game.state, game.book))

    def test_transaction_rejects_stale_platform_intent_before_any_shovel_hit(self):
        game = self.game()
        stale = Candidate('old', 'shovel', 2, 1, type_id=16, salvage=True)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(game.agent(), game.read(), candidate=stale)
        self.assertFalse(result['completed'])
        self.assertNotIn('shovel', game.actions)
        self.assertEqual({p.type_id for p in game.state.plants}, {16, 9})

    def test_nonreplacement_salvage_rechecks_source_identity_before_hit(self):
        game = self.game('source_changed')
        game.state.plants[1].hp = 100
        stale = Candidate('old', 'shovel', 2, 1, type_id=9, salvage=True,
                          source_index=1)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(game.agent(), game.read(), candidate=stale)
        self.assertFalse(result['completed'])
        self.assertNotIn('shovel', game.actions)


if __name__ == '__main__':
    unittest.main()

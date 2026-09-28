"""A pea lily is a firing base layer, never an ordinary top-layer shooter."""
import unittest
import test_strategy as fixtures
from pvz.board import Plant, Zombie
from pvz.tactics import lane_facts
from pvz.serialize import build_state


class PeaLilyTests(unittest.TestCase):
    board = fixtures.StrategyTests.board

    def setUp(self):
        fixtures.StrategyTests.setUp(self)
        self.book.bind_one(16, '睡莲')

    def test_pea_lily_defends_water_lane_and_can_support_another_plant(self):
        board = self.board([], [Zombie(0,2,0,x=650,hp=270)], [Plant(0,2,1,16)])
        board.scene=2
        board.rows=6
        self.assertGreater(lane_facts(board,2,self.book)['shooter_support'], 0)
        self.assertTrue(board.has_platform(2,1,self.book))
        self.assertNotIn((2,1), board.top_occupancy(self.book))
        self.assertTrue(board.can_plant(2,1,fixtures.PEA,self.book))
        self.assertFalse(board.can_plant(0,1,16,self.book))

    def test_model_sees_shooting_and_support_with_unverified_numbers_labelled(self):
        card = build_state(self.board([16]), self.book)['seed_cards'][0]
        self.assertIn('shooter', card['tags'])
        self.assertIn('platform', card['tags'])
        self.assertIn('pea', card['effect'].lower())
        self.assertIsNone(card['combat'].get('dps'))
        self.assertEqual(card['combat']['projectile_type'], 'pea')
        self.assertTrue(card['combat']['torch_compatible'])

    def test_user_and_upstream_names_share_same_mechanics(self):
        self.assertTrue(self.book.bind_one(416,'豌豆射手睡莲'))
        self.assertTrue(self.book.bind_one(417,'豌豆睡莲'))
        self.assertIs(self.book.kb_by_id[16], self.book.kb_by_id[416])
        self.assertIs(self.book.kb_by_id[16], self.book.kb_by_id[417])


if __name__ == '__main__':
    unittest.main()

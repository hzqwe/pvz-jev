"""Regression checks against the pinned encyclopedia, not a copied fixture."""
import unittest

from pvz.board import BoardState, SeedSlot, Zombie
from pvz.plants import PlantBook, KBEntry
from pvz.policy import generate_candidates
from pvz.serialize import build_state
from pvz.tactics import attack_dps, coverage_lanes


class ExpandedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.book.activate_catalog('classic', '3.9.9')

    def test_pinned_domain_is_loaded_with_bounded_reference_counts(self):
        domain = self.book.domain_knowledge
        self.assertGreaterEqual(len(domain.get('plants', {})), 270)
        self.assertGreaterEqual(len(domain.get('zombies', {})), 110)
        self.assertGreaterEqual(len(domain.get('relations', [])), 150)
        self.assertEqual(domain.get('identity_source_revision'),
                         'ce3363868d087363b1b69df656a582c29b099db5')

    def test_real_three_lane_profiles_use_per_lane_budget(self):
        for tid in (34, 83, 215):
            self.book.bind_identity(tid)
            with self.subTest(type_id=tid):
                self.assertEqual(coverage_lanes(self.book, tid), 3)
                self.assertGreater(attack_dps(self.book, tid), 0)
                self.assertIsNotNone(self.book.combat(tid).get('per_lane_dps'))

    def test_state_relationships_are_current_deck_bounded(self):
        for tid in (0, 86, 183):
            self.book.bind_identity(tid)
        board = BoardState(ok=True, scene=0, rows=5, cols=9, sun=1000,
                           slots=[SeedSlot(i, tid, 0, 1000)
                                  for i, tid in enumerate((0, 86, 183))],
                           zombies=[Zombie(0, 2, 5, x=700, hp=270)])
        state = build_state(board, self.book)
        self.assertLessEqual(len(state['relationships']), 8)
        self.assertLessEqual(len(state['domain_context']['enemies']), 12)
        self.assertNotIn('plants', state['domain_context'])

    def test_land_only_body_never_gets_a_pool_platform_candidate(self):
        raw = dict(en='land-only', tags=['instant'], role='instant', cost=100,
                   placement={'terrain': ['land']})
        tid = 809
        self.book.kb_by_id[tid] = KBEntry.from_json('land-only', raw)
        self.book.table[tid] = ('land-only', 100, 'instant')
        board = BoardState(ok=True, scene=2, rows=6, cols=9, sun=1000,
                           slots=[SeedSlot(0, tid, 0, 1000),
                                  SeedSlot(1, 16, 0, 1000)],
                           row_types={r: 2 for r in range(6)})
        self.assertFalse(any(c.supports_type == tid for c in generate_candidates(board, self.book)))


if __name__ == '__main__':
    unittest.main()

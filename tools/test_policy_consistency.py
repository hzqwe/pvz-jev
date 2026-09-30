"""Real catalog/state counterexamples for candidate versus execution agreement."""
import unittest

from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.plants import PlantBook
from pvz.policy import generate_candidates, action_invalid_reason
from pvz.tactics import saving_plan, can_hit


class PolicyConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook()
        self.book.activate_catalog('classic', '3.9.9')
        for tid in (9, 20, 33, 34, 46, 86, 109, 142, 147, 189, 208):
            self.assertTrue(self.book.bind_identity(tid))

    def board(self, cards, *, scene=0, plants=(), zombies=(), sun=800):
        return BoardState(ok=True, ui=3, sun=sun, rows=5, cols=9,
                          game_clock=40000, scene=scene,
                          slots=[SeedSlot(i, tid, 0, 1000) for i, tid in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies))

    def pots(self):
        return [Plant(r * 3 + c, r, c, 66) for r in range(5) for c in range(3)]

    def test_torch_upgrade_with_distant_enemy_passes_same_safety_rule(self):
        for sun in (750, 800):
            with self.subTest(sun=sun):
                b = self.board([109], plants=[Plant(1, 0, 2, 86, hp=1000),
                                              Plant(2, 0, 1, 9, hp=300)],
                               zombies=[Zombie(1, 0, 0, x=600, hp=270)], sun=sun)
                upgrades = [c for c in generate_candidates(b, self.book)
                            if c.replacement_type == 109]
                self.assertTrue(upgrades)
                self.assertIsNone(action_invalid_reason(upgrades[0], b, self.book))

    def test_ash_plant_is_never_an_opening_permanent_defence(self):
        b = self.board([9, 20, 33], scene=4, plants=self.pots(), sun=300)
        plan = saving_plan(b, self.book)
        self.assertTrue(plan is None or plan['type_id'] != 20)

    def test_corn_rush_includes_support_and_body(self):
        b = self.board([9, 34, 33], scene=4, sun=300)
        cs = [c for c in generate_candidates(b, self.book)
              if c.type_id == 34 or c.supports_type == 34]
        self.assertTrue(cs, 'Affordable flower pot + corn must be an action')
        c = max(cs, key=lambda c: c.score)
        self.assertEqual((c.type_id, c.supports_type), (33, 34))
        self.assertEqual(c.total_cost(self.book), 250)
        self.assertIsNone(action_invalid_reason(c, b, self.book))
        self.assertGreater(c.score, 120)

    def test_corn_saving_accounts_for_missing_pot(self):
        b = self.board([9, 34, 33], scene=4, sun=225)
        plan = saving_plan(b, self.book)
        self.assertEqual(plan['missing_sun'], 25)
        self.assertEqual(plan['cost'], 250)

    def test_two_corns_on_one_lane_do_not_finish_opening(self):
        b = self.board([34], scene=4, plants=self.pots() +
                       [Plant(100, 1, 0, 34), Plant(101, 1, 1, 34)], sun=300)
        self.assertEqual((saving_plan(b, self.book) or {}).get('kind'), 'corn_rush')

    def test_saved_lane_shooter_can_hit_the_actual_enemy(self):
        enemy = Zombie(1, 4, 0, x=600, hp=270)
        b = self.board([109], plants=[Plant(r, r, 0, 9, hp=300) for r in range(3)],
                       zombies=[enemy], sun=500)
        self.assertFalse(saving_plan(b, self.book)['opening'])
        cs = [c for c in generate_candidates(b, self.book) if c.type_id == 109]
        self.assertTrue(cs)
        self.assertTrue(all(can_hit(self.book, c.type_id, c.row, c.col, enemy) for c in cs))
        self.assertTrue(all(c.covers == (4,) for c in cs))

    def test_refit_cannot_bypass_multi_lane_positions(self):
        b = self.board([208], scene=4, plants=self.pots() +
                       [Plant(100, 0, 0, 9, hp=300), Plant(101, 0, 1, 9, hp=300)])
        illegal = [c for c in generate_candidates(b, self.book)
                   if c.replacement_type == 208 and c.row not in (1, 3)]
        self.assertFalse(illegal)

    def test_bound_reference_combat_remains_labelled_unverified(self):
        card = self.book.describe(34)
        self.assertIsNotNone(card['source_confidence'])
        for tid in (34, 46, 142, 147, 189):
            with self.subTest(type_id=tid):
                sources = self.book.describe(tid)['field_sources']
                self.assertIn('combat.dps', sources)
                self.assertEqual(sources['combat.dps']['confidence'], 'classic_reference')
                self.assertEqual(sources['combat.dps']['version_status'], 'unverified')

    def test_corn_estimate_declares_minimum_not_guaranteed_butter(self):
        from pvz.tactics import attack_dps, lane_dps
        combat = self.book.combat(34)
        self.assertEqual(lane_dps(self.book, 34), 10)
        self.assertEqual(attack_dps(self.book, 34), 30)
        self.assertEqual(combat.get('damage_basis'), 'kernel_only_minimum')

    def test_midgame_saving_includes_missing_roof_support(self):
        b = self.board([109, 33], scene=4, sun=500,
                       plants=[Plant(r, r, 0, 9, hp=300) for r in range(3)] +
                              [Plant(r + 10, r, 0, 66) for r in range(3)],
                       zombies=[Zombie(1, 4, 0, x=600, hp=270)])
        plan = saving_plan(b, self.book)
        self.assertEqual(plan['cost'], 575)
        self.assertEqual(plan['missing_sun'], 75)

    def test_midgame_does_not_reserve_for_unreachable_short_shooter(self):
        self.book.bind_identity(262)
        b = self.board([262], sun=500,
                       plants=[Plant(r, r, 0, 9, hp=300) for r in range(3)],
                       zombies=[Zombie(1, 4, 0, x=600, hp=270)])
        self.assertIsNone(saving_plan(b, self.book))

    def test_corn_support_chain_can_hit_adjacent_lane_enemy(self):
        enemy = Zombie(1, 0, 0, x=350, hp=270)
        b = self.board([34, 33], scene=4, sun=300, zombies=[enemy])
        cs = [c for c in generate_candidates(b, self.book)
              if c.type_id == 34 or c.supports_type == 34]
        self.assertTrue(cs)
        c = max(cs, key=lambda c: c.score)
        self.assertTrue(can_hit(self.book, 34, c.row, c.col, enemy))


if __name__ == '__main__':
    unittest.main()

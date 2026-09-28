"""Ability budgets, explicit terrain, and atomic stacked upgrades (offline)."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pvz import offsets as O
from pvz.board import BoardReader, BoardState, Plant, SeedSlot, Zombie
from pvz.plants import KBEntry, PlantBook
from pvz.policy import Candidate, action_invalid_reason, generate_candidates
from pvz.tactics import can_hit, economy_summary, lane_facts, target_dps, upgrade_value
from pvz.transactions import run_transaction
from pvz.ui import Layout


def ability_book():
    book = PlantBook(hybrid_file='', cost_file='', ids_file='')
    profiles = {
        800: dict(tags=['shooter'], combat={'dps': 90, 'coverage': '3 lanes'}),
        801: dict(tags=['producer'], combat={'sun_per_25s': 25}),
        802: dict(tags=['producer'], combat={'sun_per_25s': 100,
                 'economy': {'resource': 'currency', 'trigger': 'periodic'}}),
        803: dict(tags=['producer'], combat={'sun_per_25s': 100,
                 'economy': {'resource': 'battle_sun', 'trigger': 'sky_amplifier'}}),
        804: dict(tags=['producer'], combat={'sun_per_25s': 100,
                 'economy': {'resource': 'battle_sun', 'trigger': 'contact_trigger'}}),
        805: dict(tags=['platform'], placement={'layer': 'platform'}),
        806: dict(tags=['shooter'], placement={'terrain': ['water'], 'requires_platform': False}),
        807: dict(tags=['shooter'], placement={'terrain': ['land', 'water'], 'amphibious': True}),
        808: dict(tags=['platform'], placement={'layer': 'platform', 'support_kind': 'pot', 'terrain': ['roof']}),
    }
    for tid, raw in profiles.items():
        raw.update(en=str(tid), cost=100, role=raw['tags'][0])
        book.kb_by_id[tid] = KBEntry.from_json(str(tid), raw)
        book.table[tid] = (str(tid), 100, raw['role'])
    for tid, name in ((16, '睡莲'), (9, '阳光向日葵'), (86, '向日葵女王'),
                      (212, '狂野机枪射手')):
        book.bind_one(tid, name)
    return book


class CoverageAndEconomyTests(unittest.TestCase):
    def setUp(self):
        self.book = ability_book()

    def test_three_lane_support_reaches_adjacent_rows_without_tripling_output(self):
        board = BoardState(rows=5, plants=[Plant(0, 2, 1, 800)],
                           zombies=[Zombie(r, r, 0, x=600) for r in (1, 2, 3)])
        self.assertTrue(can_hit(self.book, 800, 2, 1, board.zombies[0]))
        self.assertFalse(can_hit(self.book, 800, 2, 1, Zombie(9, 0, 0, x=600)))
        self.assertEqual([lane_facts(board, r, self.book)['shooter_support']
                          for r in (1, 2, 3)], [1.5, 1.5, 1.5])

    def test_explicit_per_lane_damage_overrides_total_budget(self):
        self.book.kb_by_id[800].raw['combat']['per_lane_dps'] = 40
        board = BoardState(rows=5)
        self.assertEqual(target_dps(board, self.book, 800, 2, 1,
                                    Zombie(0, 1, 0, x=600)), 40)

    def test_upgrade_budget_uses_explicit_per_lane_output(self):
        self.book.kb_by_id[800].raw['combat']['per_lane_dps'] = 40
        self.assertEqual(upgrade_value(BoardState(rows=5),self.book,800,2,1),117.5)

    def test_coverage_does_not_restore_lost_edge_lane_output_or_hit_passed_enemy(self):
        board = BoardState(rows=5, plants=[Plant(0, 0, 2, 800)])
        self.assertEqual(lane_facts(board, 0, self.book)['shooter_support'], 1.5)
        self.assertEqual(lane_facts(board, 1, self.book)['shooter_support'], 1.5)
        self.assertFalse(can_hit(self.book, 800, 0, 2, Zombie(0, 1, 0, x=100)))

    def test_upgrade_value_credits_adjacent_armor_without_inventing_full_lane_damage(self):
        self.book.kb_by_id[800].raw['combat']['armor_multiplier'] = 2
        plain = BoardState(rows=5, zombies=[Zombie(0, 1, 0, x=600, hp=270)])
        armored = BoardState(rows=5, zombies=[Zombie(0, 1, 0, x=600, armor_hp=1000)])
        self.assertGreater(upgrade_value(armored, self.book, 800, 2, 1),
                           upgrade_value(plain, self.book, 800, 2, 1))

    def test_currency_and_event_income_do_not_satisfy_stable_sun_economy(self):
        board = BoardState(scene=1, plants=[Plant(i, i, 0, tid)
                           for i, tid in enumerate((801, 802, 803, 804))])
        result = economy_summary(board, self.book)
        self.assertEqual(result['producer_count'], 1)
        self.assertEqual(result['sun_per_25s_estimate'], 25)
        self.assertEqual(result['event_income_types'], [803, 804])
        self.assertEqual(result['currency_income_types'], [802])

    def test_event_currency_unknown_producers_do_not_get_rear_economy_candidates(self):
        self.book.kb_by_id[804].raw['combat'] = {}
        for tid in (802, 803, 804):
            with self.subTest(tid=tid):
                board = BoardState(ok=True, scene=1, sun=1000, game_clock=40000,
                                   slots=[SeedSlot(0, tid, 0, 100)])
                self.assertFalse(any(c.kind=='plant' and 'Safe rear economy' in c.why
                                     for c in generate_candidates(board, self.book)))
        board = BoardState(ok=True, scene=1, sun=1000, game_clock=40000,
                           slots=[SeedSlot(0, 801, 0, 100)])
        self.assertTrue(any(c.kind=='plant' and 'Safe rear economy' in c.why
                            for c in generate_candidates(board, self.book)))

    def test_unused_card_history_is_not_a_candidate_reward(self):
        board = BoardState(ok=True, rows=5, sun=1000, game_clock=40000,
                           slots=[SeedSlot(0, 801, 0, 100)])
        before = {(c.kind, c.row, c.col): c.score for c in generate_candidates(board, self.book)}
        self.book.planted_counts[801] = 1
        after = {(c.kind, c.row, c.col): c.score for c in generate_candidates(board, self.book)}
        self.assertEqual(before, after)


class ExplicitTerrainTests(unittest.TestCase):
    def setUp(self):
        self.book = ability_book()
        self.water = BoardState(scene=2, rows=6)

    def test_unknown_platform_is_not_assumed_to_support_water_or_roof(self):
        self.assertFalse(self.water.can_plant(2, 0, 805, self.book))
        self.assertFalse(BoardState(scene=4).can_plant(0, 0, 805, self.book))

    def test_water_native_and_amphibious_bodies_do_not_need_lily(self):
        self.assertTrue(self.water.can_plant(2, 0, 806, self.book))
        self.assertTrue(self.water.can_plant(2, 0, 807, self.book))
        self.assertFalse(self.water.can_plant(0, 0, 806, self.book))

    def test_water_native_card_gets_candidates_without_platform_in_deck(self):
        board = BoardState(ok=True, scene=2, rows=6, sun=1000, game_clock=40000,
                           row_types={r: 2 for r in range(6)}, slots=[SeedSlot(0, 806, 0, 100)])
        self.assertTrue(any(c.kind=='plant' and c.type_id==806
                            for c in generate_candidates(board,self.book)))

    def test_adjacent_row_candidate_can_support_a_lane_without_free_rear_cells(self):
        board = BoardState(ok=True, rows=5, sun=1000, game_clock=40000,
                           zombies=[Zombie(0,2,0,x=500,hp=2000)],
                           plants=[Plant(c,2,c,801) for c in range(6)],
                           slots=[SeedSlot(0,800,0,100)])
        self.assertTrue(any(c.kind=='plant' and c.row in (1,3) and 2 in c.covers
                            for c in generate_candidates(board,self.book)))

    def test_incompatible_existing_platform_does_not_authorize_body(self):
        roof = BoardState(scene=4, plants=[Plant(0, 0, 0, 16)])
        self.assertFalse(roof.can_plant(0, 0, 9, self.book))
        self.assertTrue(BoardState(scene=4).can_plant(0, 0, 808, self.book))

    def test_upgrade_selects_body_above_lily_and_reserves_replacement(self):
        board = BoardState(ok=True, scene=2, rows=6, sun=1000, game_clock=40000,
                           plants=[Plant(0, 2, 1, 16), Plant(1, 2, 1, 9), Plant(2, 2, 2, 86)],
                           slots=[SeedSlot(0, 212, 0, 100)])
        upgrades = [c for c in generate_candidates(board, self.book)
                    if c.kind == 'shovel' and c.salvage]
        self.assertTrue(upgrades)
        self.assertEqual(upgrades[0].type_id, 9)
        self.assertEqual(upgrades[0].replacement_type, 212)
        self.assertEqual(upgrades[0].source_index, 1)


class UpgradeGame:
    """Simulates shovel's top-body selection and real bank costs/placement."""
    def __init__(self, fault=None):
        self.book = ability_book()
        self.state = BoardState(ok=True, ui=3, scene=2, rows=6, sun=1000, game_clock=1,
            plants=[Plant(0, 2, 1, 16), Plant(1, 2, 1, 9)],
            slots=[SeedSlot(0, 212, 0, 100)])
        self.fault = fault
        self.actions = []
    def read(self):
        self.state.game_clock += 1
        return copy.deepcopy(self.state)
    def cancel_seed(self, *args):
        self.state.holding = False
        self.state.held_cursor = 0
    def shovel_hotkey(self):
        self.state.holding = True
        self.state.held_cursor = O.CUR_SHOVEL
        if self.fault == 'cooling': self.state.slots[0].cd_left = 50
        if self.fault == 'source_changed': self.state.plants[1].index = 99
        if self.fault == 'collision': self.state.plants.append(Plant(99, 2, 1, 801))
    def click_shovel(self, *args): self.shovel_hotkey()
    def click_card(self, index, *args):
        self.actions.append('pick')
        self.state.holding = True
        self.state.held_cursor = O.CUR_PLANT_FROM_BANK
        self.state.held_slot = index
        self.state.held_type = self.state.slots[index].type_id
    def click_grid(self, row, col, *args, dx=0):
        if self.state.held_cursor == O.CUR_SHOVEL:
            self.actions.append('shovel')
            top = [p for p in self.state.plants if p.cell == (row, col) and p.type_id != 16][-1]
            self.state.plants.remove(top)
        else:
            self.actions.append('plant')
            self.state.plants.append(Plant(8, row, col, self.state.held_type))
            self.state.sun -= self.book.cost(self.state.held_type)
        self.cancel_seed()
    def agent(self):
        return SimpleNamespace(reader=self, clicker=self, book=self.book, layout=Layout(), _geometry_error=None)


class ContinuousUpgradeTests(unittest.TestCase):
    def candidate(self):
        candidate = Candidate('x', 'shovel', 2, 1, type_id=9, salvage=True)
        candidate.replacement_type = 212
        candidate.source_index = 1
        return candidate

    def test_upgrade_preserves_lily_and_completes_reserved_replacement(self):
        game = UpgradeGame()
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(game.agent(), game.read(), candidate=self.candidate())
        self.assertTrue(result['completed'], result)
        self.assertEqual(game.actions, ['shovel', 'pick', 'plant'])
        self.assertEqual({p.type_id for p in game.state.plants}, {16, 212})
        self.assertIn('replacement_confirmed', [step['step'] for step in result['steps']])

    def test_shovel_does_not_destroy_source_if_replacement_cools_or_source_changes(self):
        for fault in ('cooling', 'source_changed', 'collision'):
            with self.subTest(fault=fault):
                game = UpgradeGame(fault)
                with patch('pvz.transactions.time.sleep'):
                    result = run_transaction(game.agent(), game.read(), candidate=self.candidate())
                self.assertFalse(result['completed'], result)
                self.assertNotIn('shovel', game.actions)
                self.assertTrue(any(p.type_id == 9 for p in game.state.plants))

    def test_upgrade_rejects_missing_platform_before_source_removal(self):
        game = UpgradeGame()
        game.state.plants = [game.state.plants[1]]
        self.assertIsNotNone(action_invalid_reason(self.candidate(), game.state, game.book))
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(game.agent(), game.read(), candidate=self.candidate())
        self.assertFalse(result['completed'], result)
        self.assertNotIn('shovel', game.actions)


class ShortRangeCoverageTests(unittest.TestCase):
    def setUp(self):
        self.book = ability_book()
        self.book.kb_by_id[800].raw['combat']['range_cells'] = 4

    def board(self, x=200, plants=()):
        return BoardState(ok=True, rows=5, sun=1000, game_clock=40000,
                          plants=list(plants), zombies=[Zombie(0,2,0,x=x,hp=2000)],
                          slots=[SeedSlot(0,800,0,100)])

    def test_adjacent_short_range_candidates_cannot_face_a_target_behind_them(self):
        board = self.board(plants=[Plant(c,2,c,801) for c in range(6)])
        candidates = [c for c in generate_candidates(board,self.book)
                      if c.kind=='plant' and c.type_id==800]
        self.assertTrue(candidates)
        self.assertTrue(all(can_hit(self.book,800,c.row,c.col,board.zombies[0])
                            for c in candidates))

    def test_short_range_execution_rejects_targets_that_pass_or_leave_range(self):
        candidate = Candidate('x','plant',1,1,slot=0,type_id=800,covers=(0,1,2))
        self.assertIsNone(action_invalid_reason(candidate,self.board(),self.book))
        for x in (100,700):
            with self.subTest(x=x):
                self.assertIsNotNone(action_invalid_reason(candidate,self.board(x=x),self.book))

    def test_no_enemies_allow_calm_short_range_formation(self):
        board = self.board()
        board.zombies = []
        candidates = [c for c in generate_candidates(board,self.book)
                      if c.kind=='plant' and c.type_id==800]
        self.assertTrue(candidates)
        self.assertTrue(all(action_invalid_reason(c,board,self.book) is None for c in candidates))

    def test_quiet_lanes_do_not_bypass_short_range_requirement_during_battle(self):
        self.book.kb_by_id[800].raw['combat']['range_cells'] = 2
        board = self.board(x=760)
        board.zombies[0].hp = 270
        self.assertFalse(any(c.kind=='plant' and c.type_id==800
                             for c in generate_candidates(board,self.book)))
        candidate = Candidate('x','plant',0,1,slot=0,type_id=800,covers=(0,1))
        self.assertIsNotNone(action_invalid_reason(candidate,board,self.book))


class MowerDiagnosticTests(unittest.TestCase):
    def reader(self, cap=5, count=1):
        reader = BoardReader.__new__(BoardReader)
        reader.notes = []
        reader.lawn_app_ptr = lambda: 100
        data = {1000+O.OFF_MOWER: 2000, 1000+O.OFF_MOWER_COUNT_MAX: cap,
                1000+O.OFF_MOWER_COUNT: count}
        reader.pm = SimpleNamespace(u32=lambda a: data.get(a), i32=lambda a: data.get(a), u8=lambda a: data.get(a))
        def slot(row=0, state=1, dead=0):
            data.update({2000: 100, 2004: 1000, 2000+O.M_ROW: row,
                         2000+O.M_STATE: state, 2000+O.OFF_MOWER_DEAD: dead})
        return reader, slot

    def test_invalid_pool_is_unknown_and_reports_raw_values(self):
        reader, _ = self.reader(cap=70)
        self.assertEqual(reader._read_mowers(1000, 5), {})
        self.assertEqual(getattr(reader, 'mower_diagnostics', {}).get('reason'), 'invalid_pool_header')
        self.assertEqual(reader.mower_diagnostics['capacity'], 70)
        self.assertEqual(reader.mower_diagnostics['count'], 1)
        self.assertEqual(reader.mower_diagnostics['base'], 2000)

    def test_missing_objects_remain_unknown_instead_of_consumed(self):
        reader, _ = self.reader()
        self.assertEqual(reader._read_mowers(1000, 5), {})
        self.assertEqual(getattr(reader, 'mower_diagnostics', {}).get('reason'), 'no_valid_objects')

    def test_rejected_later_object_reports_valid_objects_seen_before_it(self):
        reader, slot = self.reader()
        slot()
        old_read = reader.pm.i32
        bad = 2000+O.MOWER_STRUCT
        additions = {bad: 100, bad+4: 1000, bad+O.M_ROW: 9,
                     bad+O.M_STATE: 1, bad+O.OFF_MOWER_DEAD: 0}
        lookup = lambda a: additions.get(a, old_read(a))
        reader.pm = SimpleNamespace(u32=lookup, i32=lookup, u8=lookup)
        self.assertEqual(reader._read_mowers(1000,5), {})
        self.assertEqual(reader.mower_diagnostics['objects_seen'], 1)
        self.assertEqual(reader.mower_diagnostics['reason'], 'invalid_object')
        self.assertEqual(reader.mower_diagnostics['rejected_index'], 1)

    def test_ready_and_consumed_are_distinguishable_after_validated_read(self):
        for state, want in ((1, True), (2, False)):
            with self.subTest(state=state):
                reader, slot = self.reader()
                slot(state=state)
                self.assertEqual(reader._read_mowers(1000, 5)[0], want)
                self.assertTrue(getattr(reader, 'mower_diagnostics', {}).get('validated'))


if __name__ == '__main__':
    unittest.main()

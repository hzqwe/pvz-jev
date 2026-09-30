"""Regressions from the September 30 roof runs; no game input or API."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pvz.agent import PvZJevAgent, AgentStats
from pvz.board import BoardState, SeedSlot, Plant
from pvz.plants import PlantBook
from pvz.policy import Candidate, Decision
from pvz.ui import Layout


class InputReached(Exception):
    pass


class ExecutionReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.book.bind_one(0, '豌豆射手')
        self.book.bind_one(9, '向日葵')
        self.board = BoardState(ok=True, ui=3, scene=4, rows=5, sun=1000,
                                game_clock=100, slots=[SeedSlot(0, 0, 0, 1000)])
        self.candidate = Candidate('seed', 'plant', 0, 1, slot=0, type_id=0, score=30)

    def agent(self):
        a = PvZJevAgent.__new__(PvZJevAgent)
        def read():
            b = copy.deepcopy(self.board)
            b.game_clock += 1
            return b
        def stop(*args):
            raise InputReached()
        a.reader = SimpleNamespace(read=read)
        a.clicker = SimpleNamespace(cancel_seed=stop)
        a.layout = Layout()
        a.book = self.book
        a.stats = AgentStats()
        a.cfg = SimpleNamespace(dry_run=False, max_actions_per_min=30, verbose=False)
        a._frozen_hits = 0
        a._advances = 10
        a._action_times = []
        a._blocked_cells = {}
        a._bad_cells = {}
        a._roof_row_dx = {}
        a._geometry_error = None
        a.log_event = lambda *args, **kw: None
        a.log = SimpleNamespace(append=lambda r: None)
        return a

    def test_legacy_transaction_fault_blocks_before_any_input(self):
        a = self.agent()
        a.layout.configure_board(self.board)
        l = a.layout
        a._geometry_error = (4, 5, l.client_w, l.client_h, l.grid_left,
                             l.grid_top, l.cell_w, l.row_height())
        record = {'decision': {}}
        with patch('pvz.agent.action_is_current', return_value=True), \
                patch('pvz.agent.escalate_emergency', return_value=None):
            try:
                a.execute(Decision(candidate=self.candidate), record, self.board)
            except InputReached:
                self.fail('A confirmed transaction misplant allowed another input')
        self.assertEqual(record['executed']['kind'], 'blocked_geometry')

    def test_rapid_record_can_switch_to_rescue_without_losing_action(self):
        a = self.agent()
        rescue = Candidate('rescue', 'plant', 1, 1, slot=0, type_id=0, emergency=True)
        record = {'rapid_fill': True}
        with patch('pvz.agent.action_is_current', return_value=True), \
                patch('pvz.agent.escalate_emergency', return_value=rescue):
            try:
                a.execute(Decision(candidate=self.candidate), record, self.board)
            except InputReached:
                pass  # The fake input boundary proves the rescue reached execution.
            except KeyError as exc:
                self.fail(f'Rescue was lost before input: {exc}')
        self.assertEqual(record['decision']['action_id'], 'rescue')
        self.assertEqual(record['executed_candidate']['row'], 1)

    def test_incomplete_pad_followup_stops_rapid_fill(self):
        a = self.agent()
        records = []
        a.log.append = records.append
        pad = Candidate('pad', 'plant', 0, 0, slot=0, type_id=9, score=30)
        def partial(dec, record, board):
            record['executed'] = {'placed': True}
            record['followup'] = {'completed': False, 'kind': 'transaction_incomplete'}
        a.execute = partial
        with patch('pvz.agent.generate_candidates', return_value=[pad]):
            a.rapid_fill(self.board, count=2)
        self.assertEqual(len(records), 1, 'A failed body must stop the next purchase')

    def test_rapid_fill_records_are_replayable_and_identified(self):
        a = self.agent()
        records = []
        a.log.append = records.append
        a._battle_id = 'battle-test'
        a._code_revision = 'revision-test'
        pad = Candidate('pad', 'plant', 0, 0, slot=0, type_id=9, score=30)
        a.execute = lambda dec, rec, board: rec.update(executed={'placed': True})
        with patch('pvz.agent.generate_candidates', return_value=[pad]):
            a.rapid_fill(self.board, count=2)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['board']['scene'], 4)
        self.assertEqual(records[0]['runtime']['code_revision'], 'revision-test')
        self.assertNotEqual(records[0]['action_id'], records[1]['action_id'])

    def test_roof_sun_collection_includes_low_left_edge(self):
        from pvz.ui import memory_sun_positions
        a = self.agent()
        a.layout = Layout.load()
        a.layout.configure_board(self.board)
        coins = [{'type': 6, 'x': 80, 'y': 573, 'w': 0, 'h': 0}]
        self.assertTrue(memory_sun_positions(coins, a.layout))

    def run_misplant(self, a, row, col, actual_row, actual_col):
        initial = copy.deepcopy(self.board)
        def read():
            self.board.game_clock += 1
            return copy.deepcopy(self.board)
        def pick(index, *args):
            self.board.holding = True
            self.board.held_cursor = 1
            self.board.held_slot = index
            self.board.held_type = 0
        def place(*args, **kwargs):
            self.board.plants.append(Plant(100, actual_row, actual_col, 0, hp=300))
            self.board.sun -= 100
            self.board.holding = False
            self.board.held_cursor = 0
        a.reader = SimpleNamespace(read=read)
        a.clicker = SimpleNamespace(cancel_seed=lambda *args: None,
                                    click_card=pick, click_grid=place)
        c = Candidate('test', 'plant', row, col, slot=0, type_id=0, score=30)
        record = {}
        with patch('pvz.agent.action_is_current', return_value=True), \
                patch('pvz.agent.escalate_emergency', return_value=None), \
                patch('pvz.agent.time.sleep'):
            a.execute(Decision(candidate=c), record, initial)
        return record

    def test_other_lane_calibration_preserves_confirmed_fault(self):
        from pvz.geometry import record_geometry_failure, geometry_blocked
        a = self.agent()
        a.layout.configure_board(self.board)
        record_geometry_failure(a, self.board, 3, 4)
        self.run_misplant(a, 0, 1, 0, 2)
        self.assertTrue(a._roof_row_dx.get(0))
        self.assertTrue(geometry_blocked(a, self.board, 3, 4))

    def test_charged_cross_row_misplant_still_blocks_intended_cell(self):
        from pvz.geometry import geometry_blocked
        a = self.agent()
        record = self.run_misplant(a, 3, 4, 4, 4)
        self.assertFalse(record['executed']['placed'])
        self.assertEqual(record['executed']['sun_before'] - record['executed']['sun_after'], 100)
        self.assertTrue(geometry_blocked(a, self.board, 3, 4))

    def test_timestamped_legacy_fault_does_not_expire_into_unsafe_input(self):
        a = self.agent()
        a.layout.configure_board(self.board)
        from pvz.geometry import geometry_signature
        a._geometry_error = (geometry_signature(self.board, a.layout), 1)
        record = {}
        with patch('pvz.agent.action_is_current', return_value=True), \
                patch('pvz.agent.escalate_emergency', return_value=None):
            a.execute(Decision(candidate=self.candidate), record, self.board)
        self.assertEqual(record['executed']['kind'], 'blocked_geometry')

    def test_roof_clicks_land_inside_reference_hit_regions(self):
        # Independently checked original hit boxes mapped to the measured lawn.
        # The historical y=1166 fell in row 4; it must be inside row 3 instead.
        a = self.agent()
        a.layout = Layout.load()
        a.layout.configure_board(self.board)
        for row, col, x_expected, low, high in (
                (1, 0, 485, 672.55, 899.50),
                (3, 4, 1281, 912.85, 1139.80),
                (3, 5, 1480, 912.85, 1139.80)):
            with self.subTest(cell=(row, col)):
                x, y = a.layout.cell_center(row, col)
                self.assertEqual(x, x_expected)
                self.assertLess(low + 50, y)
                self.assertLess(y, high - 50)

    def test_confirmed_fault_is_local_and_does_not_expire_without_repair(self):
        from pvz.geometry import record_geometry_failure, geometry_blocked
        a = self.agent()
        a.layout.configure_board(self.board)
        with patch('pvz.geometry.time.time', return_value=100):
            record_geometry_failure(a, self.board, 3, 4)
        with patch('pvz.geometry.time.time', return_value=1000):
            self.assertTrue(geometry_blocked(a, self.board, 3, 4))
            self.assertFalse(geometry_blocked(a, self.board, 2, 4))
            self.assertFalse(geometry_blocked(a, self.board, 3, 5))
        a.layout.grid_top += 10
        self.assertFalse(geometry_blocked(a, self.board, 3, 4))


if __name__ == '__main__':
    unittest.main()

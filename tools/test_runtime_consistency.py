"""Offline ownership, knowledge snapshot and placement observation regressions."""
import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.board import BoardState, Plant
from pvz.plants import KBEntry, PlantBook
from pvz import serialize


class RuntimeOwnershipTests(unittest.TestCase):
    def control(self):
        self.assertIsNotNone(importlib.util.find_spec('pvz.control'),
                             'A per-game input owner is required before live input')
        from pvz.control import GameInputOwner, OwnedClicker, InputOwnershipError
        return GameInputOwner, OwnedClicker, InputOwnershipError

    def test_second_controller_cannot_acquire_and_clean_release_allows_it(self):
        Owner, _, _ = self.control()
        namespace = 'PvZJevTest-' + uuid4().hex
        first, second = Owner(900000001, namespace), Owner(900000001, namespace)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
        finally:
            first.release(); second.release()

    def test_independent_games_do_not_share_control_and_switch_releases_previous(self):
        Owner, _, _ = self.control()
        namespace = 'PvZJevTest-' + uuid4().hex
        first, old_game, other = Owner(900000001, namespace), Owner(900000001, namespace), Owner(900000002, namespace)
        try:
            self.assertTrue(first.acquire())
            self.assertTrue(other.acquire())
            first.release()
            self.assertTrue(old_game.acquire())
        finally:
            first.release(); old_game.release(); other.release()

    def test_input_proxy_never_emits_input_without_ownership(self):
        Owner, Proxy, Error = self.control()
        calls = []
        clicker = SimpleNamespace(win=SimpleNamespace(pid=900000001),
                                  click_client=lambda *args: calls.append(args))
        owner = Owner(900000001, 'PvZJevTest-' + uuid4().hex)
        proxy = Proxy(clicker, owner)
        with self.assertRaises(Error):
            proxy.click_client(10, 20, 'collect')
        self.assertEqual(calls, [])
        try:
            self.assertTrue(owner.acquire())
            proxy.click_client(10, 20, 'collect')
            self.assertEqual(calls, [(10, 20, 'collect')])
            clicker.win.pid = 900000002
            with self.assertRaises(Error):
                proxy.click_client(30, 40, 'stale window')
            self.assertEqual(len(calls), 1)
        finally:
            owner.release()

    def test_os_mutex_blocks_an_independent_process_and_recovers_after_owner_exit(self):
        Owner, _, _ = self.control()
        namespace = 'PvZJevTest-' + uuid4().hex
        code = ('from pvz.control import GameInputOwner; import sys; '
                f'o=GameInputOwner(900000001,{namespace!r}); '
                'print(o.acquire(),flush=True); sys.stdin.readline()')
        child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        owner = Owner(900000001, namespace)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'True')
            self.assertFalse(owner.acquire())
            child.terminate(); child.wait(timeout=5)
            self.assertTrue(owner.acquire())
        finally:
            if child.poll() is None:
                child.terminate(); child.wait(timeout=5)
            child.stdin.close(); child.stdout.close(); owner.release()

    def test_run_releases_control_even_when_initial_window_setup_raises(self):
        Owner, _, _ = self.control()
        from pvz.agent import PvZJevAgent, AgentConfig, AgentStats
        agent = object.__new__(PvZJevAgent)
        owner = Owner(900000001, 'PvZJevTest-' + uuid4().hex)
        self.assertTrue(owner.acquire())
        agent._input_owner = owner
        agent.cfg = AgentConfig(dry_run=False, verbose=False)
        agent.stats = AgentStats()
        agent._wd_stop = threading.Event()
        agent._seen_pid = 900000001
        with patch.object(agent, 'refresh_window', side_effect=ValueError('setup failed')):
            with self.assertRaisesRegex(ValueError, 'setup failed'):
                agent.run(duration_s=0.01)
        self.assertFalse(owner.held)

    def test_compound_click_method_cannot_bypass_the_owner(self):
        Owner, Proxy, Error = self.control()
        emitted = []
        owner = Owner(900000001, 'PvZJevTest-' + uuid4().hex)
        clicker = SimpleNamespace(win=SimpleNamespace(pid=900000001),
                                 pick_and_place=lambda *args: emitted.append(args))
        with self.assertRaises(Error):
            Proxy(clicker, owner).pick_and_place(0, 0, 0, None)
        self.assertEqual(emitted, [])

    def test_read_only_loop_never_collects_sun_even_when_clock_advances(self):
        from pvz.agent import PvZJevAgent, AgentConfig, AgentStats
        from pvz.ui import Layout
        agent = object.__new__(PvZJevAgent)
        agent.cfg = AgentConfig(dry_run=True, allow_window_ops=True, verbose=False)
        agent.stats = AgentStats(); agent._seen_pid = 900000001
        agent._window_miss = agent._dead_misses = agent._frozen_hits = 0
        agent._advances = 2; agent._binding_checked = True
        agent._last_sun = 0; agent._last_decision = 100
        agent._stop_path = 'unused'; agent.layout = Layout()
        checks = iter((False, True))
        agent._stop_requested = lambda: next(checks)
        agent._game_still_there = lambda: True
        agent.ensure_window = lambda: True
        agent.note_clock = lambda board: False
        board = BoardState(ok=True, pid=900000001, ui=3, sun=100, game_clock=20)
        agent.reader = SimpleNamespace(read=lambda: board, read_coins_raw=lambda: [])
        emitted = []
        agent.clicker = SimpleNamespace(click_client=lambda *args: emitted.append(args))
        with patch('pvz.agent.time.time', return_value=100), patch('pvz.agent.time.sleep'), \
             patch('pvz.agent.memory_sun_positions', return_value=[(10, 20, 1)]):
            agent._loop(200, 300, 300)
        self.assertEqual(emitted, [])


class CostObservationTests(unittest.TestCase):
    def book(self):
        return PlantBook(hybrid_file='', cost_file='', kb_file='', ids_file='')

    def test_known_base_cost_cannot_learn_238_from_net_sun_delta(self):
        book = self.book()
        self.assertEqual(book.cost(1), 50)
        book.set_real_cost(1, 238)
        self.assertEqual(book.cost(1), 50)
        self.assertNotIn(1, book.real_cost)

    def test_unknown_cost_observation_requires_isolated_price_evidence(self):
        book = self.book()
        self.assertTrue(hasattr(book, 'observe_placement_cost'), 'Net sun deltas need a validation boundary')
        result = book.observe_placement_cost(999, 1000, 762)
        self.assertFalse(result['accepted'])
        self.assertIsNone(book.cost(999))

    def test_dynamic_price_observation_uses_preplacement_copy_count(self):
        book = self.book()
        book.kb_by_name['dynamic'] = KBEntry.from_json('dynamic', {
            'cost': 500, 'combat': {'price_increment': 100}})
        book.bind_one(999, 'dynamic')
        book.sync_field_copies([Plant(1, 0, 0, 999), Plant(2, 1, 0, 999)])
        self.assertTrue(hasattr(book, 'observe_placement_cost'), 'Dynamic observations need validation')
        result = book.observe_placement_cost(999, 1000, 300)
        self.assertTrue(result['accepted'])
        self.assertEqual(book.real_cost[999], 500)
        self.assertEqual(book.cost(999), 700)
        bad = book.observe_placement_cost(999, 1000, 362)
        self.assertFalse(bad['accepted'])
        self.assertEqual(book.cost(999), 700)


class SnapshotReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kb = self.root / 'kb.json'
        self.ids = self.root / 'ids.json'
        self.write(self.kb, {'plants': {'producer': {'cost': 50, 'tags': ['producer'],
                    'combat': {'sun_per_25s': 25}}}})
        self.write(self.ids, {'ids': {'producer': 999}})
        self.book = PlantBook(hybrid_file='', cost_file='', kb_file=str(self.kb), ids_file=str(self.ids))

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_bound_profile_refreshes_income_and_drops_removed_tags(self):
        self.write(self.kb, {'plants': {'producer': {'cost': 50, 'tags': ['support'],
                    'combat': {'sun_per_25s': 75}}}})
        summary = self.book.reload_data()
        self.assertEqual(self.book.combat(999)['sun_per_25s'], 75)
        self.assertEqual(self.book.tags(999), ('support',))
        self.assertTrue(summary['ok'])
        self.assertGreater(summary['generation'], 0)
        self.assertEqual(len(self.book.knowledge_revision), 64)

    def test_corrupt_file_cannot_create_half_new_snapshot(self):
        old_table = dict(self.book.table)
        self.write(self.kb, {'plants': {'producer': {'cost': 99, 'combat': {'sun_per_25s': 75}}}})
        self.ids.write_text('{broken', encoding='utf-8')
        summary = self.book.reload_data()
        self.assertEqual(self.book.cost(999), 50)
        self.assertEqual(self.book.combat(999)['sun_per_25s'], 25)
        self.assertEqual(self.book.table, old_table)
        self.assertIsInstance(summary, dict, 'Reload must report failure without changing the snapshot')
        self.assertFalse(summary['ok'])

    def test_removed_profile_does_not_leave_old_automatic_mechanisms(self):
        self.write(self.kb, {'plants': {}})
        self.book.reload_data()
        self.assertNotIn('producer', self.book.kb_by_name)
        self.assertFalse(self.book.is_known(999))

    def test_manual_binding_and_confirmed_runtime_traits_survive_reload(self):
        self.book.bind_one(1001, 'producer')
        self.book.runtime_crush.add(88)
        self.book.reload_data()
        self.assertEqual(self.book.bound_ids['producer'], 1001)
        self.assertTrue(self.book.zombie_flag(88, 'crush'))
        self.assertEqual(self.book.combat(1001)['sun_per_25s'], 25)

    def test_successful_reload_invalidates_cached_playbook(self):
        playbook = self.root / 'playbook.json'
        self.write(playbook, {'principles_en': ['old']})
        with patch.object(serialize, '_PLAYBOOK_FILE', str(playbook)), patch.object(serialize, '_PLAYBOOK', None):
            self.assertEqual(serialize.load_playbook()['principles_en'], ['old'])
            self.book.reload_data()
            before = self.book.knowledge_revision
            self.write(playbook, {'principles_en': ['new']})
            self.book.reload_data()
            self.assertEqual(serialize.load_playbook()['principles_en'], ['new'])
            self.assertNotEqual(self.book.knowledge_revision, before)

    def test_hp_and_pierce_historical_fields_normalize(self):
        entry = KBEntry.from_json('wall', {'combat': {'hp': 4000, 'pierce': True}})
        self.assertEqual(entry.hp, 4000)
        self.assertTrue(entry.raw['combat']['piercing'])

    def test_catalog_effective_profile_keeps_explicit_tracking_shooter_tags(self):
        book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        book.activate_catalog('classic', '3.9.9')
        for tid in (91, 125):
            self.assertTrue(book.bind_identity(tid))
            self.assertIn('shooter', book.tags(tid))
            self.assertIn('tracking', book.tags(tid))

    def test_reload_does_not_promote_automatic_cost_into_prior_version_facts(self):
        book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        book.real_cost[86] = 450  # fact recorded before automatic version binding
        book.activate_catalog('classic', '3.9.9'); book.bind_identity(86)
        book.real_cost[86] = 500
        result = book.reload_data()
        self.assertTrue(result['ok'])
        book.deactivate_catalog()
        self.assertEqual(book.real_cost[86], 450)

    def test_invalid_shape_keeps_complete_old_snapshot(self):
        self.write(self.kb, {'plants': ['invalid']})
        result = self.book.reload_data()
        self.assertFalse(result['ok'])
        self.assertEqual(self.book.combat(999)['sun_per_25s'], 25)

    def test_invalid_binding_record_is_rejected_without_dropping_old_binding(self):
        self.write(self.ids, {'ids': {'producer': 'not_an_id'}})
        result = self.book.reload_data()
        self.assertFalse(result['ok'])
        self.assertEqual(self.book.combat(999)['sun_per_25s'], 25)

    def test_event_producers_cannot_publish_fake_fixed_sun_rate(self):
        book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        book.activate_catalog('classic', '3.9.9')
        for tid, trigger in ((155, 'sky_amplifier'), (160, 'contact_trigger')):
            self.assertTrue(book.bind_identity(tid))
            combat = book.combat(tid)
            self.assertNotIn('sun_per_25s', combat)
            self.assertEqual(combat['economy']['trigger'], trigger)

    def test_loaded_cost_mismatch_is_quarantined_after_catalog_binding(self):
        costs = self.root / 'costs.json'
        self.write(costs, {'91': 238})
        book = PlantBook(hybrid_file='', cost_file=str(costs), ids_file='')
        book.activate_catalog('classic', '3.9.9')
        self.assertTrue(book.bind_identity(91))
        self.assertEqual(book.cost(91), book.kb_by_id[91].cost)
        self.assertNotIn(91, book.real_cost)


class DomainProfileTests(unittest.TestCase):
    def test_reference_price_requires_three_independent_isolated_observations(self):
        book = PlantBook(hybrid_file='', cost_file='', kb_file='', ids_file='')
        book.kb_by_name['reference'] = KBEntry.from_json('reference', {
            'cost': 50, 'field_sources': {'cost': {'confidence': 'classic_reference'}}})
        book.bind_one(999, 'reference')
        book.domain_knowledge = {'plants': {'999': {'profile': {'cost': 50}}}}
        self.assertTrue(hasattr(book, 'reference_cost_evidence'), 'Reference-only prices need an evidence queue')
        for action_id in ('a1', 'a2'):
            result = book.observe_placement_cost(999, 500, 425, isolated=True, observation_id=action_id)
            self.assertFalse(result['accepted'])
            self.assertEqual(book.cost(999), 50)
        repeated = book.observe_placement_cost(999, 500, 425, isolated=True, observation_id='a2')
        self.assertFalse(repeated['accepted'])
        polluted = book.observe_placement_cost(999, 500, 262, observation_id='a3')
        self.assertFalse(polluted['accepted'])
        result = book.observe_placement_cost(999, 500, 425, isolated=True, observation_id='a3')
        self.assertTrue(result['accepted'])
        self.assertEqual(book.cost(999), 75)
        repeated_confirmed = book.observe_placement_cost(999, 500, 425)
        self.assertTrue(repeated_confirmed['accepted'])

    def test_hybrid_profile_cannot_inherit_original_short_range_from_same_id(self):
        book = PlantBook(hybrid_file='', cost_file='', kb_file='', ids_file='')
        book.activate_catalog('classic', '3.9.9')
        book.kb_by_name['hybrid'] = KBEntry.from_json('hybrid', {'role': 'shooter'})
        book.bind_one(10, 'hybrid')
        self.assertIsNone(book.range_cells(10))

    def test_optional_domain_profile_is_id_verified_and_keeps_user_confirmed_cost(self):
        import inspect
        self.assertIn('encyclopedia_file', inspect.signature(PlantBook).parameters,
                      'Domain profiles require explicit optional input and identity validation')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'encyclopedia.json'
            path.write_text(json.dumps({'schema_version': 1, 'edition': 'classic',
                'game_version': '3.9.9',
                'identity_source_revision': 'ce3363868d087363b1b69df656a582c29b099db5',
                'plants': {'0': {'canonical_name': '豌豆向日葵', 'profile': {
                    'cost': 999, 'placement': {'terrain': ['land', 'roof'], 'layer': 'body'},
                    'field_sources': {'cost': {'confidence': 'classic_reference', 'source_url': 'https://example.org/pea'},
                                      'placement': {'confidence': 'classic_reference', 'source_url': 'https://example.org/pea'}}}}},
                'zombies': {}, 'relations': [], 'scenes': {}, 'sources': {}}), encoding='utf-8')
            book = PlantBook(hybrid_file='', cost_file='', ids_file='', encyclopedia_file=str(path))
            book.activate_catalog('classic', '3.9.9'); book.bind_identity(0)
            self.assertEqual(book.cost(0), 125)
            self.assertEqual(book.placement(0)['layer'], 'body')
            self.assertIn('plants', book.domain_knowledge)
            before = book.describe(0)
            doc = json.loads(path.read_text(encoding='utf-8'))
            doc['plants']['0']['canonical_name'] = 'wrong identity'
            path.write_text(json.dumps(doc), encoding='utf-8')
            result = book.reload_data()
            self.assertFalse(result['ok'])
            self.assertEqual(book.describe(0), before)


class RoofCalibrationTests(unittest.TestCase):
    def test_reliable_one_column_miss_applies_reverse_offset_and_is_bounded(self):
        from pvz.agent import PvZJevAgent
        from pvz.ui import Layout
        agent = object.__new__(PvZJevAgent)
        agent.layout = Layout()
        agent._roof_row_dx = {}
        before = BoardState(ok=True, pid=900000001, board=1, scene=4, rows=5, game_clock=10)
        after = copy.deepcopy(before); after.game_clock = 20
        self.assertTrue(hasattr(agent, '_calibrate_roof_offset'), 'Calibration needs bounded evidence validation')
        self.assertTrue(agent._calibrate_roof_offset(0, 2, [(0, 3)], before, after))
        self.assertEqual(agent._roof_row_dx[0], -80)
        self.assertFalse(agent._calibrate_roof_offset(0, 2, [(0, 5)], before, after))
        self.assertEqual(agent._roof_row_dx[0], -80)
        self.assertFalse(agent._calibrate_roof_offset(0, 2, [(1, 3)], before, after))


if __name__ == '__main__':
    unittest.main()

"""The audit reports missing mechanics without changing runtime facts."""
import copy
import json
import sys
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.catalog import DEFAULT_CATALOG, load_catalog
from pvz.plants import PlantBook
from tools.audit_knowledge import audit_book
from pvz.agent import PvZJevAgent
from pvz.board import BoardState, SeedSlot
from pvz.jev import DecisionLog
from pvz.policy import generate_candidates
from pvz.serialize import build_state


class KnowledgeAuditTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(str(DEFAULT_CATALOG), 'classic', '3.9.9')
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.book.bind_one(0, '豌豆射手')
        self.book.bind_one(16, '睡莲')

    def test_audit_preserves_bindings_prices_and_unconfirmed_traits(self):
        self.book.real_cost[0] = 125
        self.book.zombie_traits['types'][88] = {'crush': True, 'confirmed': False}
        before = copy.deepcopy(self.book.__dict__)
        report = audit_book(self.catalog, self.book, [16,0])
        self.assertEqual(self.book.__dict__, before)
        self.assertEqual(report['aliases'][0]['local_name'], '睡莲')
        self.assertEqual(report['aliases'][0]['canonical_name'], '豌豆睡莲')
        self.assertIn(88, report['unconfirmed_zombie_traits'])

    def test_identity_only_and_unknown_cards_have_no_invented_mechanics(self):
        report = audit_book(self.catalog, self.book, [67,9999,16])
        rows = {r['type_id']:r for r in report['cards']}
        self.assertTrue(rows[67]['identity_known'])
        self.assertFalse(rows[67]['mechanics_known'])
        self.assertFalse(rows[9999]['identity_known'])
        self.assertFalse(rows[9999]['mechanics_known'])
        self.assertTrue(rows[16]['mechanics_known'])
        self.assertEqual(report['missing_mechanics'], [67,9999])
        self.assertEqual(report['counts']['deck'], 3)
        self.assertEqual(rows[67]['missing_fields'], ['cost','hp','cooldown_s','role','combat'])

    def test_deck_order_does_not_reassign_identity(self):
        for ids in ([67,16,86], [86,67,16]):
            rows = audit_book(self.catalog, self.book, ids)['cards']
            names = {r['type_id']:r['canonical_name'] for r in rows}
            self.assertEqual(names, {67:'荷叶',16:'豌豆睡莲',86:'向日葵女王'})

    def test_available_mechanics_match_runtime_without_binding_or_mutation(self):
        book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        before = copy.deepcopy(book.__dict__)
        offline = audit_book(self.catalog,book,[0,86,183])
        self.assertEqual(book.__dict__,before)
        self.assertEqual(offline['counts']['mechanics_available'],3)
        self.assertEqual(offline['counts']['mechanics_bound'],0)
        book.activate_catalog('classic','3.9.9')
        for tid in (0,86,183):
            book.bind_identity(tid)
        online = audit_book(self.catalog,book,[0,86,183])
        self.assertEqual(online['counts']['mechanics_bound'],3)
        for a,b in zip(offline['cards'],online['cards']):
            self.assertEqual(a['missing_fields'],b['missing_fields'])
            self.assertEqual(a['inferred_fields'],b['inferred_fields'])
            self.assertEqual(a['unverified_fields'],b['unverified_fields'])

    def test_inferred_income_and_conflicting_claims_are_explicit(self):
        report = audit_book(self.catalog,self.book,[0,86])
        pea,queen = report['cards']
        self.assertIn('combat.sun_per_25s',pea['inferred_fields'])
        self.assertEqual(pea['unverified_fields']['shot_dps']['value'],20)
        self.assertIn('freeze_immunity',queen['unverified_fields'])
        self.assertTrue(any(c['type_id']==86 and c['field']=='freeze_immunity'
                            for c in report['conflicts']))

    def test_wrong_version_and_absent_version_are_rejected(self):
        for edition, version in [('classic','3.19'),('remake','0.28'),('classic','')]:
            with self.subTest(edition=edition,version=version), self.assertRaises(ValueError):
                load_catalog(str(DEFAULT_CATALOG), edition, version)
        raw = json.loads(DEFAULT_CATALOG.read_text(encoding='utf-8'))
        del raw['game_version']
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'catalog.json'
            path.write_text(json.dumps(raw), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_catalog(str(path), 'classic','3.9.9')

    def test_agent_recognizes_reordered_deck_and_logs_missing_mechanics(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = PvZJevAgent.__new__(PvZJevAgent)
            agent.book = self.book
            agent._binding_checked = agent._binding_warned = False
            agent.log = DecisionLog(str(Path(folder)/'decision.jsonl'))
            # Avoid constructing an API client or interacting with a game window.
            board = BoardState(ok=True, sun=1000, rows=5, cols=9,
                               slots=[SeedSlot(i,t,0,1000) for i,t in enumerate([67,86,16])])
            with redirect_stdout(io.StringIO()):
                agent.configure_catalog(SimpleNamespace(pid=123,title='植物大战僵尸杂交版v3.9.9'))
                agent.sync_binding(board)
            self.assertEqual(agent.book.name(67), '荷叶')
            self.assertEqual(agent.book.name(86), '向日葵女王')
            self.assertFalse(agent.book.is_known(67))
            self.assertFalse(any(c.type_id==67 for c in generate_candidates(board,agent.book)))
            event = json.loads(Path(agent.log.event_path).read_text(encoding='utf-8').splitlines()[-1])
            self.assertEqual(event['event'], 'knowledge_coverage')
            self.assertEqual(event['missing_mechanics'], [67])
            seeds = build_state(board,agent.book)['seed_cards']
            self.assertEqual(seeds[0]['knowledge_status'], 'identity_only')
            with redirect_stdout(io.StringIO()):
                agent.configure_catalog(SimpleNamespace(pid=124,title='植物大战僵尸杂交版v3.19'))
            self.assertIsNone(agent.book.identity(67))
            self.assertNotIn(86, agent.book.kb_by_id)
            self.assertFalse(agent.book.is_known(86))


if __name__ == '__main__':
    unittest.main()

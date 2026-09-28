"""The audit reports missing mechanics without changing runtime facts."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.catalog import DEFAULT_CATALOG, load_catalog
from pvz.plants import PlantBook
from tools.audit_knowledge import audit_book


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


if __name__ == '__main__':
    unittest.main()

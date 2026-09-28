"""Version identity contracts: wrong editions, unsafe source and shifted IDs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.catalog import extract_literal_names, load_catalog, version_from_title
from pvz.plants import PlantBook

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'data/versions/classic-3.9.9/catalog.json'


class SourceImportTests(unittest.TestCase):
    def test_only_initial_names_are_imported_without_running_source(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'executed'
            source = (f'open({str(marker)!r}, "w").write("bad")\n'
                      'plantsType = ["豌豆向日葵", "阳光豆"]\n'
                      'for _ in range(510): plantsType.append("占位")\n'
                      'plantsType = plantsType + ["普僵"]\n')
            self.assertEqual(extract_literal_names(source, 'plantsType'),
                             ('豌豆向日葵', '阳光豆'))
            self.assertFalse(marker.exists())

    def test_dynamic_initializer_is_rejected(self):
        for source in ['plantsType = get_names()', 'plantsType = ["a", str(1)]']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                extract_literal_names(source, 'plantsType')

    def test_redefined_initial_list_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_literal_names('plantsType = ["a"]\nplantsType = ["b"]', 'plantsType')

    def test_missing_or_empty_names_are_rejected(self):
        for source in ['other = ["a"]', 'plantsType = [""]', 'plantsType = []']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                extract_literal_names(source, 'plantsType')


class PinnedIdentityTests(unittest.TestCase):
    def test_real_card_ids_do_not_shift_with_duplicate_names(self):
        catalog = load_catalog(str(PACK), 'classic', '3.9.9')
        for tid, name in {0:'豌豆向日葵', 16:'豌豆睡莲', 67:'荷叶',
                          86:'向日葵女王', 161:'回收高坚果', 183:'雷果子'}.items():
            with self.subTest(tid=tid):
                self.assertEqual(catalog.resolve('plant', tid).canonical_name, name)
        self.assertEqual(catalog.resolve('zombie', 5).canonical_name, '冰车二爷')
        self.assertIsNone(catalog.resolve('plant', 9999))

    def test_imported_identity_is_not_a_combat_profile(self):
        raw = json.loads(PACK.read_text(encoding='utf-8'))
        self.assertEqual(raw['source_revision'], 'ce3363868d087363b1b69df656a582c29b099db5')
        self.assertEqual(raw['entities']['plant']['16']['mechanics_status'], 'identity_only')
        self.assertNotIn('dps', raw['entities']['plant']['16'])


class RuntimeIdentityTests(unittest.TestCase):
    def book(self):
        return PlantBook(hybrid_file='', cost_file='', ids_file='')

    def test_new_card_names_do_not_inherit_original_pvz_mechanics(self):
        book = self.book()
        book.activate_catalog('classic', '3.9.9')
        self.assertEqual(book.name(33), '阳光花盆')
        self.assertEqual(book.identity(33).canonical_name, '阳光花盆')
        self.assertFalse(book.is_known(33))
        self.assertEqual(book.tags(33), ('unknown',))
        self.assertIsNone(book.cost(33))
        self.assertIsNone(book.range_cells(10))
        self.assertEqual(book.describe(33)['knowledge_status'], 'identity_only')

    def test_verified_bindings_and_learned_costs_survive_activation(self):
        book = self.book()
        book.bind_one(0, '豌豆射手')
        book.real_cost[0] = 125
        book.activate_catalog('classic','3.9.9')
        self.assertEqual(book.name(0), '豌豆射手')
        self.assertEqual(book.cost(0), 125)
        self.assertTrue(book.is_known(0))

    def test_inactive_or_wrong_version_cannot_supply_catalog_identity(self):
        book = self.book()
        self.assertIsNone(book.identity(67))
        book.activate_catalog('classic','3.9.9')
        with self.assertRaises(ValueError):
            book.activate_catalog('remake','0.28')
        self.assertIsNone(book.identity(67))

    def test_runtime_title_requires_unambiguous_classic_version(self):
        self.assertEqual(version_from_title('植物大战僵尸杂交版v3.9.9'), ('classic','3.9.9'))
        self.assertIsNone(version_from_title('植物大战僵尸杂交重制版v0.28'))
        self.assertIsNone(version_from_title('Plants vs. Zombies'))

    def test_reordered_known_mechanics_bind_by_identity_without_lineup(self):
        book = self.book()
        book.activate_catalog('classic','3.9.9')
        for tid in [86,16,161]:
            self.assertTrue(book.bind_identity(tid))
        self.assertEqual(book.name(86), '向日葵女王')
        self.assertEqual(book.name(161), '回收高坚果')
        self.assertFalse(book.bind_identity(67))


if __name__ == '__main__':
    unittest.main()

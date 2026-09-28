"""Version identity contracts: wrong editions, unsafe source and shifted IDs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.catalog import extract_literal_names, load_catalog

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


if __name__ == '__main__':
    unittest.main()

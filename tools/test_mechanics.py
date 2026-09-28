"""Version-specific facts must not overwrite verified legacy profiles/prices."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.catalog import DEFAULT_CATALOG, load_catalog, load_profile_bindings
from pvz.plants import PlantBook


class MechanicsTests(unittest.TestCase):
    def book(self):
        book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        book.bind_one(0, '豌豆射手')
        book.real_cost[0] = 125
        return book

    def test_pea_income_is_version_scoped_and_preserves_price_and_original_profile(self):
        book = self.book()
        original = book.kb_by_id[0]
        before = copy.deepcopy(original.raw)
        book.activate_catalog('classic', '3.9.9')
        self.assertIn('producer', book.tags(0))
        self.assertIn('shooter', book.tags(0))
        self.assertEqual(book.combat(0)['sun_per_25s'], 25)
        self.assertEqual(book.cost(0), 125)
        self.assertEqual(book.name(0), '豌豆射手')
        self.assertEqual(book.combat(0)['dps'], before['combat']['dps'])
        self.assertEqual(original.raw, before)
        self.assertIsNot(book.kb_by_id[0], original)
        book.deactivate_catalog()
        self.assertIs(book.kb_by_id[0], original)
        self.assertEqual(book.cost(0), 125)

    def test_new_binding_receives_beam_capability_without_changed_prices(self):
        book = self.book()
        book.activate_catalog('classic', '3.9.9')
        self.assertTrue(book.bind_identity(183))
        self.assertTrue(book.combat(183).get('piercing'))
        self.assertEqual(book.combat(183)['projectile_type'], 'beam')
        self.assertEqual(book.price_increment(183), 100)
        self.assertFalse(book.bind_identity(228))
        self.assertNotIn('producer', book.tags(228))

    def test_cross_version_claim_is_reported_outside_active_combat(self):
        book = self.book()
        book.activate_catalog('classic', '3.9.9')
        book.bind_identity(86)
        self.assertNotIn('v4.0', book.combat(86)['notes'])
        self.assertIn('freeze_immunity', book.kb_by_id[86].raw['unverified_fields'])

    def test_readonly_catalog_entry_does_not_change_book(self):
        book = self.book()
        catalog = load_catalog(str(DEFAULT_CATALOG), 'classic', '3.9.9')
        before = copy.deepcopy(book.__dict__)
        entry = book.catalog_entry(0, catalog)
        self.assertIn('producer', entry.tags)
        self.assertEqual(book.__dict__, before)
        self.assertIsNone(book.catalog_entry(228, catalog))

    def test_mechanics_reject_wrong_identity_and_missing_field_evidence(self):
        from pvz.mechanics import load_mechanics
        catalog = load_catalog(str(DEFAULT_CATALOG), 'classic', '3.9.9')
        profiles = load_profile_bindings(catalog)
        raw = json.loads(DEFAULT_CATALOG.with_name('mechanics.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'mechanics.json'
            for mutate in (lambda d:d.update(game_version='3.19'),
                           lambda d:d['plants']['0'].update(canonical_name='豌豆射手'),
                           lambda d:d['plants']['0'].update(field_sources={})):
                bad = copy.deepcopy(raw)
                mutate(bad)
                path.write_text(json.dumps(bad), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_mechanics(catalog, profiles, path)


if __name__ == '__main__':
    unittest.main()

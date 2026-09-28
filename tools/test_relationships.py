"""Relationships supplied to Jev must be applicable, bounded and evidence-labelled."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.board import BoardState, Plant, SeedSlot, Zombie
try:
    from pvz.relations import relationship_summary, domain_context
except ImportError:
    relationship_summary = domain_context = None
try:
    from pvz.knowledge import domain_coverage
except ImportError:
    domain_coverage = None


class Book:
    def __init__(self, relations):
        self.domain_knowledge = {'relations': relations, 'scenes': {}, 'zombies': {}}
        self.kb_by_id = {0: object(), 86: object(), 161: object(), 183: object()}
    def tags(self, tid):
        return {0: ['shooter'], 86: ['torch'], 161: ['wall'], 183: ['shooter']}.get(tid, [])
    def has_tag(self, tid, tag): return tag in self.tags(tid)
    def combat(self, tid): return {'torch_compatible': tid == 0}
    def zombie_flag(self, tid, key): return tid == 5 and key == 'crush'


def relation(name='pea_fire', **extra):
    return dict(id=name, kind='torch_path', plant_ids=[0, 86],
                reason_en='Put compatible peas behind the torch.', conditions={},
                source_url='https://pvzhe.wiki/w/向日葵女王', confidence='user_confirmed', **extra)


class RelationshipTests(unittest.TestCase):
    def test_coverage_distinguishes_basic_reference_from_computable_attack(self):
        self.assertTrue(callable(domain_coverage), 'domain_coverage is missing')
        book = Book([])
        book.domain_knowledge['plants'] = {
            '0': {'canonical_name': '豌豆向日葵', 'profile': {'cost': 125, 'hp': 300,
                'tags': ['shooter'], 'combat': {'dps': 14}}},
            '1': {'canonical_name': '阳光豆', 'profile': {'cost': 50, 'hp': 300,
                'tags': [], 'combat': {'notes': 'Special target operation is not implemented.'}}}}
        counts = domain_coverage(book)
        self.assertEqual(counts['plant_reference_records'], 2)
        self.assertEqual(counts['plant_quantitative_attack_records'], 1)
        self.assertEqual(counts['plant_reference_only_records'], 1)

    def summary(self, board, book, **kw):
        self.assertTrue(callable(relationship_summary), 'relationship_summary is missing')
        return relationship_summary(board, book, **kw)

    def test_unselected_partner_does_not_enter_state(self):
        board = BoardState(ok=True, slots=[SeedSlot(0, 0, 0, 100)])
        self.assertEqual(self.summary(board, Book([relation()])), [])

    def test_ready_deck_pair_can_be_planned_without_observed_synergy(self):
        board = BoardState(ok=True, slots=[SeedSlot(0, 0, 0, 100), SeedSlot(1, 86, 0, 100)])
        summary = self.summary(board, Book([relation()]))
        self.assertEqual(summary[0]['status'], 'available_to_plan')
        self.assertEqual(summary[0]['active_lanes'], [])
        self.assertEqual(summary[0]['confidence'], 'user_confirmed')

    def test_fire_path_requires_pea_behind_live_same_row_torch(self):
        book = Book([relation()])
        b = BoardState(ok=True, plants=[Plant(0, 0, 1, 0), Plant(1, 0, 2, 86)])
        self.assertEqual(self.summary(b, book)[0]['active_lanes'], [1])
        b.plants[0].col = 3
        self.assertEqual(self.summary(b, book)[0]['active_lanes'], [])
        b.plants[0].col = 1
        b.plants[1].asleep = True
        self.assertEqual(self.summary(b, book)[0]['active_lanes'], [])

    def test_terrain_and_current_enemy_conditions_filter_relations(self):
        item = relation()
        item['conditions'] = {'terrain': 'water', 'enemy_traits': ['crush']}
        book = Book([item])
        b = BoardState(ok=True, scene=0, slots=[SeedSlot(0, 0, 0, 100), SeedSlot(1, 86, 0, 100)])
        self.assertEqual(self.summary(b, book), [])
        b.scene = 2; b.zombies = [Zombie(0, 2, 5, x=700)]
        self.assertEqual(len(self.summary(b, book)), 1)
        b.zombies[0].friendly = True
        self.assertEqual(self.summary(b, book), [])

    def test_limit_and_duplicate_ids_bound_large_encyclopedia(self):
        book = Book([relation(str(i)) for i in range(80)] + [relation('0')])
        b = BoardState(ok=True, slots=[SeedSlot(0, 0, 0, 100), SeedSlot(1, 86, 0, 100)])
        self.assertEqual(len(self.summary(b, book)), 8)
        self.assertEqual(len(self.summary(b, book, limit=2)), 2)
        self.assertEqual(self.summary(b, book, limit=0), [])

    def test_bad_unsourced_records_are_not_treated_as_instructions(self):
        item = relation(); item.pop('source_url')
        book = Book([None, {'plant_ids': '0'}, item])
        b = BoardState(ok=True, slots=[SeedSlot(0, 0, 0, 100), SeedSlot(1, 86, 0, 100)])
        self.assertEqual(self.summary(b, book), [])

    def test_observed_pair_survives_limit_after_many_planning_hints(self):
        records = [dict(relation(str(i)), kind='other_advice') for i in range(20)]
        records.append(relation('active_pair'))
        b = BoardState(ok=True, plants=[Plant(0, 0, 1, 0), Plant(1, 0, 2, 86)])
        hints = self.summary(b, Book(records))
        self.assertEqual(len(hints), 8)
        self.assertEqual(hints[0]['id'], 'active_pair')

    def test_malformed_reference_sections_do_not_break_state(self):
        book = Book([dict(relation(), conditions={'terrain': ['water']})])
        book.domain_knowledge.update(scenes=[], zombies=[])
        b = BoardState(ok=True, slots=[SeedSlot(0, 0, 0, 100), SeedSlot(1, 86, 0, 100)],
                       zombies=[Zombie(0, 0, 5, x=700)])
        self.assertEqual(self.summary(b, book), [])
        self.assertEqual(domain_context(b, book)['enemies'], [])

    def test_current_scene_and_enemy_context_excludes_entire_catalog(self):
        self.assertTrue(callable(domain_context), 'domain_context is missing')
        book = Book([])
        book.domain_knowledge['scenes'] = {'0': {'name_en': 'Lawn'}, '4': {'name_en': 'Roof'}}
        book.domain_knowledge['zombies'] = {
            '5': {'canonical_name': '冰车二爷', 'traits': {'crush': True}, 'field_sources': {'crush': {'confidence': 'classic_reference'}}},
            '12': {'canonical_name': '冰车巨人', 'traits': {'crush': True}}}
        b = BoardState(ok=True, scene=0, zombies=[Zombie(0, 0, 5, x=700, hp=100)])
        context = domain_context(b, book)
        self.assertEqual(context['scene']['name_en'], 'Lawn')
        self.assertEqual([x['type_id'] for x in context['enemies']], [5])
        self.assertFalse(context['enemies'][0]['confirmed'])


if __name__ == '__main__': unittest.main()

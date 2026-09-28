"""Read-only coverage report; never promote identities into combat facts."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.catalog import DEFAULT_CATALOG, VersionCatalog, load_catalog
from pvz.plants import PlantBook


def audit_book(catalog: VersionCatalog, book: PlantBook, deck_ids: list[int]) -> dict:
    cards, aliases, conflicts = [], [], []
    for tid in dict.fromkeys(t for t in deck_ids if t >= 0):
        identity = catalog.resolve('plant', tid)
        entry = book.kb_by_id.get(tid)
        local_name = entry.cn if entry else None
        canonical = identity.canonical_name if identity else None
        row = dict(type_id=tid, local_name=local_name, canonical_name=canonical,
                   identity_known=identity is not None, mechanics_known=entry is not None)
        if entry:
            # Static facts only: book.cost() expires runtime floors as a side effect.
            values = {'cost': entry.cost, 'hp': entry.hp, 'cooldown_s': entry.cooldown_s,
                      'role': entry.role, 'combat': entry.raw.get('combat')}
            row['missing_fields'] = [k for k,v in values.items() if v is None or v == '' or v == {}]
            if 'shooter' in entry.tags and (entry.raw.get('combat') or {}).get('dps') is None:
                row['missing_fields'].append('attack_dps')
            notes = str((entry.raw.get('combat') or {}).get('notes', ''))
            if 'v4.0' in notes:
                conflicts.append(dict(type_id=tid, field='combat.notes', reason='source_version_unverified'))
        else:
            row['missing_fields'] = ['cost','hp','cooldown_s','role','combat']
        if identity and local_name and local_name != canonical:
            aliases.append(dict(type_id=tid, local_name=local_name, canonical_name=canonical,
                                acknowledged=local_name in identity.aliases))
        cards.append(row)
    return dict(edition=catalog.edition, game_version=catalog.game_version,
                source_revision=catalog.source_revision, cards=cards, aliases=aliases,
                conflicts=conflicts,
                counts={'deck':len(cards), 'identity_known':sum(c['identity_known'] for c in cards),
                        'mechanics_known':sum(c['mechanics_known'] for c in cards)},
                missing_identity=sorted(c['type_id'] for c in cards if not c['identity_known']),
                missing_mechanics=sorted(c['type_id'] for c in cards if not c['mechanics_known']),
                unconfirmed_zombie_traits=sorted(tid for tid, facts in book.zombie_traits['types'].items()
                                                if facts.get('confirmed') is not True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edition', default='classic')
    parser.add_argument('--game-version', default='3.9.9')
    parser.add_argument('--catalog', type=Path, default=DEFAULT_CATALOG)
    parser.add_argument('--deck', type=int, nargs='*')
    args = parser.parse_args()
    catalog = load_catalog(str(args.catalog), args.edition, args.game_version)
    book = PlantBook()
    deck = args.deck if args.deck is not None else list(book.bound_ids.values())
    print(json.dumps(audit_book(catalog, book, deck), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

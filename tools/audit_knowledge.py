"""Read-only coverage report; never promote identities into combat facts."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.catalog import DEFAULT_CATALOG, VersionCatalog, load_catalog
from pvz.plants import PlantBook


from pvz.knowledge import audit_book


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

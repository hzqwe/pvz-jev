"""Offline sourced-knowledge coverage; no API key, game or external requests."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.plants import PlantBook
from pvz.knowledge import audit_book, domain_coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deck', type=int, nargs='*')
    args = parser.parse_args()
    book = PlantBook(hybrid_file='', cost_file='', ids_file='')
    book.activate_catalog('classic', '3.9.9')
    result = dict(knowledge_revision=getattr(book, 'knowledge_revision', None),
                  coverage=domain_coverage(book))
    if args.deck is not None:
        for tid in args.deck:
            book.bind_identity(tid)
        result['deck'] = audit_book(book.catalog, book, args.deck)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()

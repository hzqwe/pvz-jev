"""Offline, static import of the pinned MIT identity tables (never import source)."""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.catalog import REVISION, extract_literal_names

SOURCE_SHA256 = '0edd3be9cc8340c7c22185378fa0c3f1883516f99d3facd893a253f896d4adcc'
SOURCE_URL = f'https://github.com/EFrostBlade/PVZHybrid_Editor/blob/{REVISION}/PVZ_data.py'


def import_catalog(source_path: Path, license_path: Path, output_dir: Path) -> dict:
    source_bytes = source_path.read_bytes()
    digest = hashlib.sha256(source_bytes).hexdigest()
    if digest != SOURCE_SHA256:
        raise ValueError('Source content differs from the reviewed beta 0.66 snapshot')
    source = source_bytes.decode('utf-8')
    entities = {}
    for kind, variable in [('plant', 'plantsType'), ('zombie', 'zombiesType')]:
        names = extract_literal_names(source, variable)
        entities[kind] = {str(tid): {'canonical_name': name, 'aliases': [],
                                    'mechanics_status': 'identity_only'}
                          for tid, name in enumerate(names) if name != '占位'}
    # User-confirmed alias; the original upstream name and source stay intact.
    entities['plant']['16']['aliases'] = ['睡莲', '豌豆射手睡莲']
    pack = dict(schema_version=1, edition='classic', game_version='3.9.9',
                source_revision=REVISION, entities=entities)
    provenance = dict(edition='classic', game_version='3.9.9', tag='β0.66',
                      source_revision=REVISION, source_url=SOURCE_URL,
                      source_sha256=digest, license='MIT',
                      imported_at=datetime.now(timezone.utc).isoformat(),
                      warning='Identity candidates, not selectable-entity counts or verified mechanics.',
                      alias_evidence={'plant:16': 'User confirmed pea-shooting lily identity 2026-09-28'})
    license_text = license_path.read_text(encoding='utf-8')
    if 'MIT License' not in license_text or 'EFrostBlade' not in license_text:
        raise ValueError('Missing upstream MIT attribution')
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in [('catalog.json', pack), ('sources.json', provenance)]:
        (output_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (output_dir / 'LICENSE.upstream.txt').write_text(license_text, encoding='utf-8')
    return {kind: len(records) for kind, records in entities.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--license-file', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=Path('data/versions/classic-3.9.9'))
    args = parser.parse_args()
    print(json.dumps(import_catalog(args.source, args.license_file, args.output_dir), ensure_ascii=False))


if __name__ == '__main__':
    main()

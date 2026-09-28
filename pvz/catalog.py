"""Version-scoped identities. An identity never supplies combat mechanics."""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

REVISION = 'ce3363868d087363b1b69df656a582c29b099db5'
DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / 'data/versions/classic-3.9.9/catalog.json'


def _literal_names(value: ast.AST) -> tuple[str, ...]:
    if not isinstance(value, ast.List) or not value.elts:
        raise ValueError('Expected a non-empty literal name list')
    if any(not isinstance(v, ast.Constant) or not isinstance(v.value, str)
           or not v.value.strip() for v in value.elts):
        raise ValueError('Names must be non-empty string literals')
    return tuple(v.value for v in value.elts)


def extract_literal_names(source: str, variable: str) -> tuple[str, ...]:
    """Read one initial list; ignore the upstream's subsequent card extension."""
    found = None
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or not any(
                isinstance(t, ast.Name) and t.id == variable for t in node.targets):
            continue
        v = node.value
        if (found is not None and isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add)
                and isinstance(v.left, ast.Name) and v.left.id == variable):
            _literal_names(v.right)  # documented zombie-card suffix is not a plant list
            continue
        if found is not None:
            raise ValueError(f'Duplicate or dynamic definition of {variable}')
        found = _literal_names(v)
    if found is None:
        raise ValueError(f'Missing initial list: {variable}')
    return found


@dataclass(frozen=True)
class CatalogIdentity:
    edition: str
    game_version: str
    kind: str
    type_id: int
    canonical_name: str
    aliases: tuple[str, ...] = ()


@dataclass
class VersionCatalog:
    edition: str
    game_version: str
    source_revision: str
    entities: dict[str, dict[int, CatalogIdentity]]

    def resolve(self, kind: str, type_id: int) -> CatalogIdentity | None:
        return self.entities.get(kind, {}).get(type_id)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def load_catalog(path: str, edition: str, game_version: str) -> VersionCatalog:
    data = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_unique_keys)
    if (edition, game_version) != ('classic', '3.9.9') or (
            data.get('edition'), data.get('game_version')) != (edition, game_version):
        raise ValueError('Identity catalog does not match the verified game version')
    if data.get('source_revision') != REVISION or data.get('schema_version') != 1:
        raise ValueError('Unsupported catalog source/schema')
    raw = data.get('entities')
    if not isinstance(raw, dict) or set(raw) != {'plant', 'zombie'}:
        raise ValueError('Catalog must contain separate plant and zombie identities')
    entities = {}
    for kind, records in raw.items():
        if not isinstance(records, dict):
            raise ValueError('Identity records must be keyed by type_id')
        entries = {}
        for key, row in records.items():
            if not re.fullmatch(r'0|[1-9]\d*', key) or not isinstance(row, dict):
                raise ValueError('Invalid identity key/record')
            name, aliases = row.get('canonical_name'), row.get('aliases', [])
            if (not isinstance(name, str) or not name.strip() or
                    not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases)
                    or row.get('mechanics_status') != 'identity_only'):
                raise ValueError('Invalid identity name/status')
            entries[int(key)] = CatalogIdentity(edition, game_version, kind, int(key), name, tuple(aliases))
        entities[kind] = entries
    return VersionCatalog(edition, game_version, data['source_revision'], entities)


def version_from_title(title: str) -> tuple[str, str] | None:
    """Recognize only the classic title shape; a generic PvZ window is ambiguous."""
    match = re.fullmatch(r'植物大战僵尸杂交版\s*[vV](\d+\.\d+(?:\.\d+)?)', title.strip())
    return ('classic', match[1]) if match else None


def load_profile_bindings(catalog: VersionCatalog) -> dict[int, str]:
    """Explicit legacy profile assignments; names alone cannot establish mechanics."""
    path = DEFAULT_CATALOG.with_name('mechanics_bindings.json')
    data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_keys)
    if (data.get('schema_version') != 1 or
            (data.get('edition'), data.get('game_version')) != (catalog.edition, catalog.game_version) or
            data.get('identity_source_revision') != catalog.source_revision):
        raise ValueError('Mechanics bindings do not match the identity namespace')
    rows = data.get('profiles')
    if not isinstance(rows, dict):
        raise ValueError('Missing explicit mechanics bindings')
    profiles = {}
    for key, row in rows.items():
        if not re.fullmatch(r'0|[1-9]\d*', key) or not isinstance(row, dict):
            raise ValueError('Invalid mechanics binding')
        identity = catalog.resolve('plant', int(key))
        profile = row.get('profile_name')
        if (identity is None or row.get('canonical_name') != identity.canonical_name or
                not isinstance(profile, str) or not profile.strip()):
            raise ValueError('Mechanics binding has an identity conflict')
        profiles[int(key)] = profile
    return profiles

"""Small, evidenced field overlays; never apply a current wiki wholesale."""
import copy
import json
import math
import re
from pathlib import Path
from .catalog import DEFAULT_CATALOG, _unique_keys


def load_mechanics(catalog, profiles, path=None):
    path = Path(path) if path is not None else DEFAULT_CATALOG.with_name('mechanics.json')
    data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_keys)
    if (data.get('schema_version') != 1 or
            (data.get('edition'), data.get('game_version')) != (catalog.edition, catalog.game_version) or
            data.get('identity_source_revision') != catalog.source_revision):
        raise ValueError('Mechanics overlay namespace mismatch')
    records = data.get('plants')
    if not isinstance(records, dict):
        raise ValueError('Missing mechanics records')
    result = {}
    for key, record in records.items():
        if not re.fullmatch(r'0|[1-9]\d*', key) or not isinstance(record, dict):
            raise ValueError('Invalid mechanics identity')
        tid = int(key)
        identity = catalog.resolve('plant', tid)
        if (identity is None or identity.canonical_name != record.get('canonical_name') or
                profiles.get(tid) != record.get('profile_name')):
            raise ValueError('Mechanics identity/profile conflict')
        patch, evidence = record.get('patch'), record.get('field_sources')
        if not isinstance(patch, dict) or not isinstance(evidence, dict) or not patch:
            raise ValueError('Missing patch/evidence')
        paths = []
        for field, value in patch.items():
            if field == 'combat' and isinstance(value, dict):
                for name, item in value.items():
                    valid = (name in ('notes', 'projectile_type') and isinstance(item, str) or
                             name == 'piercing' and isinstance(item, bool) or
                             name == 'sun_per_25s' and not isinstance(item, bool) and
                             isinstance(item, (int, float)) and math.isfinite(item) and item > 0)
                    if not valid:
                        raise ValueError('Unsupported/unverified combat field')
                    paths.append('combat.' + name)
            elif field == 'tags' and isinstance(value, list) and all(isinstance(t, str) for t in value):
                paths.append(field)
            elif field in ('en', 'effect_en') and isinstance(value, str):
                paths.append(field)
            else:
                # Prices/HP/cooldowns and shot numbers require a separate validation path.
                raise ValueError('Overlay cannot overwrite this field')
        for field in paths:
            source = evidence.get(field)
            if (not isinstance(source, dict) or not source.get('confidence') or
                    source.get('applicable_version') != 'classic-3.9.9' or
                    not (source.get('source_url') or source.get('local_evidence'))):
                raise ValueError('Mechanics field lacks versioned evidence')
        result[tid] = copy.deepcopy(record)
    return result


def overlay(raw, record):
    result = copy.deepcopy(raw)
    for key, value in record['patch'].items():
        if key == 'combat':
            result.setdefault(key, {}).update(copy.deepcopy(value))
        else:
            result[key] = copy.deepcopy(value)
    result.setdefault('field_sources', {}).update(copy.deepcopy(record['field_sources']))
    result.setdefault('unverified_fields', {}).update(copy.deepcopy(record.get('unverified_fields', {})))
    return result

"""Read-only identity and mechanics coverage for runtime and offline audits."""
from __future__ import annotations
import copy
from typing import TYPE_CHECKING
from .catalog import VersionCatalog
if TYPE_CHECKING:
    from .plants import PlantBook


def domain_coverage(book) -> dict:
    """Reference count is not complete mechanic support or verified-version count."""
    data = getattr(book, 'domain_knowledge', {}) or {}
    plants = data.get('plants', {})
    profiles = [record.get('profile', {}) for record in plants.values() if isinstance(record, dict)]
    attacks = sum(isinstance(p.get('combat', {}).get('dps'), (int, float))
                  and not isinstance(p.get('combat', {}).get('dps'), bool)
                  and p['combat']['dps'] > 0 for p in profiles)
    supported = sum(bool(p.get('tags')) and p.get('cost') is not None
                    and bool(p.get('combat') or p.get('placement')) for p in profiles)
    return dict(plant_reference_records=len(plants), plant_quantitative_attack_records=attacks,
                plant_rule_profile_records=supported,
                plant_reference_only_records=len(profiles)-supported,
                zombie_reference_records=len(data.get('zombies', {})),
                relation_records=len(data.get('relations', [])),
                scene_records=len(data.get('scenes', {})),
                note='Reference fields may be version-unverified. Rule profiles are partial, not complete gameplay support or measured performance.')


def audit_book(catalog: VersionCatalog, book: PlantBook, deck_ids: list[int]) -> dict:
    cards, aliases, conflicts = [], [], []
    for tid in dict.fromkeys(t for t in deck_ids if t >= 0):
        identity = catalog.resolve('plant', tid)
        entry = book.catalog_entry(tid,catalog)
        local_name = entry.cn if entry else None
        canonical = identity.canonical_name if identity else None
        row = dict(type_id=tid, local_name=local_name, canonical_name=canonical,
                   identity_known=identity is not None, mechanics_known=entry is not None,
                   mechanics_available=entry is not None,
                   mechanics_bound=tid in book.kb_by_id,
                   inferred_fields=[],unverified_fields={})
        if entry:
            row['inferred_fields'] = sorted(k for k,v in entry.raw.get('field_sources',{}).items()
                                            if v.get('confidence')=='classic_inferred')
            row['unverified_fields'] = copy.deepcopy(entry.raw.get('unverified_fields',{}))
            for field,source in row['unverified_fields'].items():
                conflicts.append(dict(type_id=tid,field=field,
                                      reason=source.get('reason','source_version_unverified')))
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
                        'mechanics_known':sum(c['mechanics_known'] for c in cards),
                        'mechanics_available':sum(c['mechanics_available'] for c in cards),
                        'mechanics_bound':sum(c['mechanics_bound'] for c in cards)},
                missing_identity=sorted(c['type_id'] for c in cards if not c['identity_known']),
                missing_mechanics=sorted(c['type_id'] for c in cards if not c['mechanics_known']),
                unconfirmed_zombie_traits=sorted(tid for tid, facts in book.zombie_traits['types'].items()
                                                if facts.get('confirmed') is not True))

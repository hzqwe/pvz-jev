"""Bounded, relevant almanac relationships; observations are separate from advice.

Exact combat and legality remain in tactics/board. This module never treats a
source's suggested synergy as measured damage, or retrieves anything at runtime.
"""


def _knowledge(book):
    value = getattr(book, 'domain_knowledge', {})
    return value if isinstance(value, dict) else {}


def _available(board, book):
    return {p.type_id for p in board.plants} | {s.type_id for s in board.slots}


def _terrain(board):
    types = {'water' if board.is_water(r) else 'roof' if board.scene == 4 else 'land'
             for r in range(board.rows)}
    return types


def _active_lanes(item, board, book):
    ids = set(item['plant_ids'])
    plants = [p for p in board.plants if p.type_id in ids and not p.asleep and p.hp != 0]
    kind = item['kind']
    if kind == 'torch_path':
        return sorted({p.row + 1 for p in plants
                       if book.combat(p.type_id).get('torch_compatible')
                       for torch in plants if book.has_tag(torch.type_id, 'torch')
                       and p.row == torch.row and p.col < torch.col})
    if kind in ('wall_fire', 'wall_shooter'):
        return sorted({p.row + 1 for p in plants if book.has_tag(p.type_id, 'shooter')
                       for wall in plants if book.has_tag(wall.type_id, 'wall')
                       and p.row == wall.row and p.col < wall.col})
    return []  # Other effects need observations not represented in the snapshot.


def relationship_summary(board, book, limit=8):
    """Return sourced pair advice applicable to the current deck, at most 8 items."""
    limit = max(0, min(8, int(limit)))
    available = _available(board, book)
    result, seen = [], set()
    records = _knowledge(book).get('relations', [])
    if not isinstance(records, list) or not limit:
        return result
    for item in records:
        if not isinstance(item, dict):
            continue
        ids, identity = item.get('plant_ids'), item.get('id')
        if (not isinstance(identity, str) or identity in seen
                or not isinstance(ids, list) or not ids
                or any(not isinstance(t, int) or isinstance(t, bool) or t < 0 for t in ids)
                or not set(ids).issubset(available)
                or not isinstance(item.get('kind'), str)
                or not isinstance(item.get('reason_en'), str)
                or not item.get('confidence')
                or not (item.get('source_url') or item.get('local_evidence'))):
            continue
        conditions = item.get('conditions') or {}
        if not isinstance(conditions, dict):
            continue
        terrain = conditions.get('terrain')
        if terrain and (not isinstance(terrain, str) or terrain not in _terrain(board)):
            continue
        traits = conditions.get('enemy_traits') or []
        if not isinstance(traits, list) or any(not isinstance(t, str) for t in traits):
            continue
        if traits and not any(not z.friendly and all(book.zombie_flag(z.type_id, t) for t in traits)
                              for z in board.zombies):
            continue
        lanes = _active_lanes(item, board, book)
        result.append(dict(id=identity, kind=item['kind'], plant_ids=ids,
                           reason=item['reason_en'][:220], active_lanes=lanes,
                           status='observed_geometry' if lanes else 'available_to_plan',
                           confidence=item['confidence'],
                           version_status=item.get('version_status', 'see_field_sources'),
                           source_url=item.get('source_url')))
        seen.add(identity)
    # Keep an observed formation even when many earlier records merely suggest
    # future pairings. Stable order resolves ties without adding model work.
    result.sort(key=lambda item: not bool(item['active_lanes']))
    return result[:limit]


def domain_context(board, book):
    """Only the active scene and current enemies, never the complete catalogue."""
    data = _knowledge(book)
    scenes = data.get('scenes', {})
    zombie_records = data.get('zombies', {})
    scenes = scenes if isinstance(scenes, dict) else {}
    zombie_records = zombie_records if isinstance(zombie_records, dict) else {}
    scene = scenes.get(str(board.scene), {})
    scene = {k: v for k, v in scene.items() if k in
             ('name_en', 'requires_support', 'projectile_note_en', 'economy_note_en',
              'source_url', 'confidence', 'version_status')} if isinstance(scene, dict) else {}
    enemies = []
    for tid in sorted({z.type_id for z in board.zombies if not z.friendly})[:12]:
        record = zombie_records.get(str(tid), {})
        if not isinstance(record, dict) or not record:
            continue
        traits = record.get('traits') or {}
        sources = record.get('field_sources') or {}
        if not isinstance(traits, dict) or not isinstance(sources, dict):
            continue
        # The raw book keeps full provenance; the request carries one compact
        # provenance summary and the actual observed health elsewhere in lanes.
        confidence = sorted({s.get('confidence', 'unknown') for s in sources.values()
                             if isinstance(s, dict)})
        enemies.append(dict(type_id=tid, name=record.get('canonical_name'),
                            traits={k: v[:220] if isinstance(v, str) else v
                                    for k, v in traits.items()
                                    if isinstance(v, (str, int, float, bool)) and k != 'confirmed'},
                            confirmed=traits.get('confirmed') is True,
                            confidence=confidence or ['unverified'],
                            source_url=record.get('source_url') or next(
                                (s.get('source_url') for s in sources.values()
                                 if isinstance(s, dict) and s.get('source_url')), None)))
    return dict(scene=scene, enemies=enemies,
                note='Reference traits may be version-unverified; observed health/armor and confirmed runtime traits take precedence.')

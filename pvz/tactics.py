"""Shared, deterministic tactical facts. Scores are heuristics, not predicted DPS."""
from .plants import T_WALL, T_SHOOTER, T_TRACKING, T_PRODUCER, T_TEMPORARY


def cell_x(col):
    return 80 + 80 * col


def strength(z):
    # Unknown hybrid IDs are never translated using the plant catalogue.
    return max(1.0, min(30.0, ((z.hp if z.hp is not None else 270) +
                              (z.armor_hp or 0)) / 270))


def attack_dps(book, type_id):
    profile = book.combat(type_id)
    if T_TEMPORARY in book.tags(type_id):
        return 0  # charm is situational control, not permanent damage
    default = 14.0 if book.has_tag(type_id, T_SHOOTER) else 0
    return profile.get('dps', default) * profile.get('hit_factor', 1)


def upgrade_value(board, book, type_id, row=None):
    """Contextual utility, not a combat simulator or a reward for expensive cards."""
    profile, tags = book.combat(type_id), book.tags(type_id)
    groups = [board.zombies_in_lane(r) for r in range(board.rows)]
    active = sum(bool(zs) for zs in groups)
    if row is None:
        row = max(range(board.rows), key=lambda r: sum(strength(z) for z in groups[r]))
    tracking = T_TRACKING in tags
    targets = [z for group in groups for z in group] if tracking else groups[row]
    armored = sum(bool(z.armor_hp) for z in targets) / max(1,len(targets))
    dps = attack_dps(book,type_id)
    value = dps * (1 + armored*(profile.get('armor_multiplier',1)-1))
    if profile.get('spread'):
        value *= 1 + .25*min(3,max(0,len(targets)-1))
    if tracking:
        value *= 1 + .1*max(0,active-1)
    # Avoid endlessly buying the same control: credit only uncovered pressured lanes.
    def has_control(r, key):
        return any(not p.asleep and (book.combat(p.type_id).get(key) or
                   key=='stun' and book.combat(p.type_id).get('stun_chance')) and
                   (book.has_tag(p.type_id,T_TRACKING) or p.row==r and any(
                       z.x is not None and cell_x(p.col)<z.x for z in groups[r]))
                   for p in board.plants)
    affected = [r for r in range(board.rows) if groups[r]] if tracking else [row]
    if profile.get('slow'):
        value += sum(16 if profile.get('splash') else 8 for r in affected if not has_control(r,'slow'))
    if profile.get('stun') or profile.get('stun_chance'):
        value += sum(5 for r in affected if not has_control(r,'stun'))
    if T_WALL in tags and targets:
        blocking = any(book.has_tag(p.type_id,T_WALL) and any(
            z.x is not None and cell_x(p.col)<=z.x for z in groups[row])
            for p in board.plants_in_lane(row))
        if not blocking:
            value += min(40,(book.hp(type_id) or 0)/150)
            value += 20*armored if profile.get('armor_multiplier',1)>1 else 0
        if any(z.x is not None and z.x < 400 for z in targets):
            value += profile.get('reflect_dps',0)*.3
    if profile.get('sun_per_25s'):
        income = sum(book.combat(p.type_id).get('sun_per_25s',0)
                     for p in board.plants if not p.asleep)
        value += profile['sun_per_25s'] * max(.1,.4-income/1500)
    copies = sum(p.type_id==type_id and not p.asleep for p in board.plants)
    return round(value/(1+.25*copies) - (book.cost(type_id) or 0)*.025,2)


def rear_cols(board, book, row, producer=False):
    """Only empty squares behind the first wall and ahead of no passed zombie."""
    ps = board.plants_in_lane(row)
    wall = min((p.col for p in ps if book.has_tag(p.type_id, T_WALL)), default=board.cols)
    nx = min((z.x for z in board.zombies_in_lane(row) if z.x is not None), default=9999)
    hi = min(3 if producer else 5, wall - 1)
    occ = board.occupancy()
    return [c for c in range(hi + 1) if (row, c) not in occ and cell_x(c) + 35 < nx]


def lane_facts(board, row, book=None):
    zs = board.zombies_in_lane(row)
    ps = board.plants_in_lane(row)
    nx = min((z.x for z in zs if z.x is not None), default=None)
    power = sum(strength(z) for z in zs)
    mower = board.mowers.get(row)
    shooters, walls = 0.0, 0
    if book:
        for p in board.plants:
            if p.asleep:
                continue
            tags = book.tags(p.type_id)
            if T_SHOOTER in tags and (T_TRACKING in tags or
                    (p.row == row and (nx is None or cell_x(p.col) < nx))):
                share = 1
                if T_TRACKING in tags:
                    total_strength = sum(strength(z) for r in range(board.rows) for z in board.zombies_in_lane(r))
                    share = power/total_strength if total_strength else 0
                shooters += attack_dps(book,p.type_id)/20 * share
            if p.row == row and T_WALL in tags and (nx is None or cell_x(p.col) <= nx):
                walls += 1
    critical = bool(zs) and nx is not None and (nx < 160 or (mower is False and nx < 240 and walls == 0))
    high = bool(zs) and (critical or (nx is not None and nx < 320) or power > shooters * 3 + 5)
    level = 'critical' if critical else 'high' if high else 'low' if zs else 'none'
    pressure = max(0, power - shooters * 1.5 - walls * 2)
    proximity = max(0, (800 - nx) / 8) if nx is not None else 40
    priority = (1000 if critical else 200 if high else 0) + proximity + pressure * 8
    if mower is False:
        priority = priority * 1.35 + 30
    if not zs:
        priority = 0
    return dict(lane=row + 1, zombie_count=len(zs), nearest_zombie_x=nx,
                nearest_closeness=level, plant_count=len(ps), threat_level=level,
                priority=round(priority, 2), zombie_strength=round(power, 2),
                shooter_support=round(shooters, 2), blocking_walls=walls,
                mower_available=mower, pressure=round(pressure, 2))


def saving_plan(board, book):
    """Reserve for a usable upgrade, but release the reserve when defence is urgent."""
    facts = [lane_facts(board, r, book) for r in range(board.rows)]
    producers = sum(book.has_tag(p.type_id, T_PRODUCER) for p in board.plants)
    if producers < 4 or not any(f['zombie_count'] for f in facts) or any(
            f['threat_level'] in ('critical', 'high') for f in facts):
        return None
    heavy = sum(not p.asleep and book.has_tag(p.type_id, T_SHOOTER)
                and not book.has_tag(p.type_id, T_TEMPORARY)
                and (book.cost(p.type_id) or 0) >= 300
                and (book.has_tag(p.type_id,T_TRACKING) or any(
                    z.x is not None and cell_x(p.col) < z.x for z in board.zombies_in_lane(p.row)))
                for p in board.plants)
    if heavy >= min(3, max(1, int(sum(f['zombie_strength'] for f in facts) / 6) + 1)):
        return None
    options = []
    for s in board.slots:
        tags, cost = book.tags(s.type_id), book.cost(s.type_id)
        if (T_SHOOTER not in tags or T_TEMPORARY in tags or
                cost is None or cost < 300 or s.cooldown_left_frac > 0.5):
            continue
        rows = [r for r in range(board.rows) if rear_cols(board, book, r) and
                (T_TRACKING in tags or facts[r]['zombie_count']) and
                (T_WALL not in tags or not facts[r]['blocking_walls'] or facts[r]['pressure']>0)]
        if rows:
            options.append((-upgrade_value(board,book,s.type_id), cost, s.index, s.type_id))
    if not options:
        return None
    _, cost, slot, tid = min(options)
    return dict(type_id=tid, slot=slot, plant=book.en(tid), cost=cost,
                missing_sun=max(0, cost - (board.sun or 0)),
                utility=upgrade_value(board,book,tid),
                reason='Choose the upgrade matching coverage, armor, control and economy needs; not simply the cheapest card.')

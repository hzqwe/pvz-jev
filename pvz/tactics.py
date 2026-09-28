"""Shared, deterministic tactical facts. Scores are heuristics, not predicted DPS."""
import time

from .plants import T_WALL, T_SHOOTER, T_TRACKING, T_PRODUCER, T_TEMPORARY, T_TORCH


def cell_x(col):
    return 80 + 80 * col


def crusher_approaching(board, book, source):
    """Source-specific asset-loss warning, independent of nearest-enemy ranking.

    A confirmed hostile crusher within two cells may instantly destroy even a
    full-health wall. The contact margin permits recovery as overlap begins;
    trucks already past the plant must not cause pointless healthy-wall removal.
    """
    profile = book.combat(source.type_id)
    if profile.get('crush_hits') or profile.get('lethal_hit_burst'):
        return False
    return any(z.x is not None and not z.friendly
               and book.zombie_flag(z.type_id, 'crush')
               and -40 <= z.x - cell_x(source.col) <= 160
               for z in board.zombies_in_lane(source.row))


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


def can_hit(book, type_id, row, col, target):
    """Forward-lane geometry; tracking targets do not need a straight-line path."""
    if target.friendly or not book.has_tag(type_id, T_SHOOTER):
        return False
    if book.has_tag(type_id, T_TRACKING):
        return True
    if target.row != row or target.x is None or target.x < cell_x(col):
        return False
    reach = book.range_cells(type_id)
    return reach is None or target.x-cell_x(col) <= reach*80


def torch_path(board, book, type_id, row, col, target=None):
    """A known straight projectile crosses a live torch before reaching its target."""
    if (not book.combat(type_id).get('torch_compatible') or
            book.has_tag(type_id, T_TRACKING) or
            target is not None and not can_hit(book,type_id,row,col,target)):
        return False
    return any(p.row==row and p.col>col and not p.asleep and p.hp!=0
               and book.has_tag(p.type_id,T_TORCH)
               and (target is None or target.x is not None and cell_x(p.col)<target.x)
               for p in board.plants)


def target_dps(board, book, type_id, row, col, target):
    """Conservative target estimate; unverified torch multipliers add no damage."""
    if not can_hit(book,type_id,row,col,target):
        return 0.0
    dps = attack_dps(book,type_id)
    profile = book.combat(type_id)
    armor = target.armor_hp or 0
    body = target.hp if target.hp is not None else 270
    multiplier = profile.get('armor_multiplier',1)
    if armor and multiplier>1:
        # Double damage to armor cannot also double damage to the unarmored body.
        dps *= (body+armor)/(body+armor/multiplier)
    if book.has_tag(type_id,T_TRACKING):
        total = sum(strength(z) for r in range(board.rows) for z in board.zombies_in_lane(r))
        dps *= min(1,strength(target)/max(1,total))
    return dps


def economy_summary(board, book):
    income, count, inferred, unknown = 0.0, 0, set(), set()
    for p in board.plants:
        if p.asleep or p.hp==0 or not book.has_tag(p.type_id,T_PRODUCER):
            continue
        count += 1
        amount = book.combat(p.type_id).get('sun_per_25s')
        if amount is None:
            unknown.add(p.type_id)
        else:
            income += amount  # No observed plant age: retain the young-plant rate.
        entry = book.kb_by_id.get(p.type_id)
        source = (entry.raw.get('field_sources',{}).get('combat.sun_per_25s',{}) if entry else {})
        if source.get('confidence')=='classic_inferred':
            inferred.add(p.type_id)
    return dict(producer_count=count, sun_per_25s_estimate=round(income,2),
                inferred_income_types=sorted(inferred), unknown_income_types=sorted(unknown),
                note='Planning estimate. Growth ages and next payout times are unobserved; inferred rates are not measured.')


def upgrade_value(board, book, type_id, row=None, col=None):
    """Contextual utility, not a combat simulator or a reward for expensive cards."""
    profile, tags = book.combat(type_id), book.tags(type_id)
    groups = [board.zombies_in_lane(r) for r in range(board.rows)]
    active = sum(bool(zs) for zs in groups)
    if row is None:
        row = max(range(board.rows), key=lambda r: sum(strength(z) for z in groups[r]))
    tracking = T_TRACKING in tags
    targets = [z for group in groups for z in group] if tracking else groups[row]
    if col is not None and not tracking:
        targets = [z for z in targets if can_hit(book,type_id,row,col,z)]
    armored = sum(bool(z.armor_hp) for z in targets) / max(1,len(targets))
    dps = attack_dps(book,type_id)
    value = dps * (1 + armored*(profile.get('armor_multiplier',1)-1))
    if profile.get('spread'):
        value *= 1 + .25*min(3,max(0,len(targets)-1))
    if profile.get('piercing') and not tracking and col is not None:
        # Saving estimates have no chosen cell: do not promise crowd hits.
        value *= 1 + .35*min(4,max(0,len(targets)-1))
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
    if profile.get('mature_sun_per_25s'):
        # 会长大的产能（阳光向日葵）：成熟期收入不保证（生长年龄无法观测），
        # 只给一个打折的小额信用，不当成确定收入。
        value += profile['mature_sun_per_25s'] * .15
    if profile.get('aura_dps'):
        # 灼烧光环只打植物周围 3x3，不是全屏：只统计还在光环射程内
        # （草坪左侧约三格，x<480）且与落点同行/邻排的僵尸。
        radius = profile.get('aura_radius_cells', 1)
        near = sum(1 for z in targets if z.x is not None and z.x < 480
                   and abs(z.row - row) <= radius)
        if near:
            value += profile['aura_dps'] * .2 * min(near, 4)
    if profile.get('death_freeze') and targets:
        # 亡语控制：这面墙倒下时也会冻结/减速周围僵尸，属于免费的兜底控制。
        value += 6
    copies = sum(p.type_id==type_id and not p.asleep for p in board.plants)
    return round(value/(1+.25*copies) - (book.cost(type_id) or 0)*.025,2)


def rear_cols(board, book, row, producer=False):
    """Only empty squares behind the first wall and ahead of no passed zombie."""
    ps = board.plants_in_lane(row)
    wall = min((p.col for p in ps if book.has_tag(p.type_id, T_WALL)), default=board.cols)
    nx = min((z.x for z in board.zombies_in_lane(row) if z.x is not None), default=9999)
    hi = min(3 if producer else 5, wall - 1)
    occ = board.top_occupancy(book)
    cols = [c for c in range(hi + 1) if (row, c) not in occ and cell_x(c) + 35 < nx
            and not board.snow_blocked(row,c) and not board.placement_blocked(row,c)]
    if board.is_water(row):
        pads = [c for c in cols if board.has_platform(row,c,book)]
        # Reuse a paid platform before planning another one.
        if pads:
            return pads
        if not any(s.ready and book.has_tag(s.type_id,'platform')
                   and book.cost(s.type_id) is not None
                   and book.cost(s.type_id) <= (board.sun or 0) for s in board.slots):
            return []
    return cols


def lane_facts(board, row, book=None):
    zs = board.zombies_in_lane(row)
    ps = board.plants_in_lane(row)
    nx = min((z.x for z in zs if z.x is not None), default=None)
    power = sum(strength(z) for z in zs)
    mower = board.mowers.get(row)
    shooters, walls, reflect = 0.0, 0, 0.0
    if book:
        for p in board.plants:
            if p.asleep or p.hp==0:
                continue
            tags = book.tags(p.type_id)
            if T_SHOOTER in tags:
                if not zs and p.row==row and T_TRACKING not in tags:
                    # No target exists yet: keep standing formation power for
                    # quiet construction, without projecting damage at an enemy.
                    shooters += attack_dps(book,p.type_id)/20
                elif T_TRACKING in tags:
                    shooters += sum(target_dps(board,book,p.type_id,p.row,p.col,z) for z in zs)/20
                elif p.row==row and nx is not None:
                    lead = min((z for z in zs if z.x is not None),key=lambda z:z.x)
                    # A forward beam may clear the crowd, but cannot save a lead
                    # enemy that already passed the firing plant.
                    if can_hit(book,p.type_id,p.row,p.col,lead):
                        targets = zs if book.combat(p.type_id).get('piercing') else [lead]
                        shooters += sum(target_dps(board,book,p.type_id,p.row,p.col,z) for z in targets)/20
            if p.row == row and T_WALL in tags and (nx is None or cell_x(p.col) <= nx):
                walls += 1
                # 反伤只在僵尸真的啃到墙时生效：僵尸从右往左走，追上墙
                # （nx <= 墙列 x + 少量容差）才算接触，远处还没走到的不算。
                if nx is not None and nx <= cell_x(p.col) + 30:
                    reflect += book.combat(p.type_id).get('reflect_dps', 0) / 20
    critical = bool(zs) and nx is not None and (nx < 160 or (mower is False and nx < 240 and walls == 0))
    high = bool(zs) and (critical or (nx is not None and nx < 320) or power > shooters * 3 + 5)
    level = 'critical' if critical else 'high' if high else 'low' if zs else 'none'
    pressure = max(0, power - shooters * 1.5 - walls * 2 - reflect * 1.0)
    proximity = max(0, (800 - nx) / 8) if nx is not None else 40
    priority = (1000 if critical else 200 if high else 0) + proximity + pressure * 8
    if mower is False:
        priority = priority * 1.35 + 30
    if not zs:
        priority = 0
    # 行为/特征威胁（2026-09-26 进阶）：远程驻停僵尸点杀无墙保护的昂贵植物，
    # 撞车僵尸直接压扁不可防撞的墙 —— 两者都会改变墙/灰烬候选的优先级。
    # ranged 来自 agent 的跨快照驻停观测；crush/eat_dps 来自 zombie_traits.json。
    ranged = sum(1 for z in zs if getattr(z, 'stationary', None))
    crush = sum(1 for z in zs if book is not None and book.zombie_flag(z.type_id, 'crush'))
    # 最近的那只僵尸是不是撞车（2026-09-26 用户实战）：普通墙挡不住冰车，
    # 最近僵尸=冰车时种普通墙=白给 —— policy 据此禁止该路的普通墙候选。
    nearest_z = min(zs, key=lambda z: z.x) if zs else None
    nearest_is_crush = bool(nearest_z is not None and book is not None
                            and book.zombie_flag(nearest_z.type_id, 'crush'))
    eat_dps = max([book.zombie_trait(z.type_id, 'eat_dps') or 100
                   for z in zs] or [100]) if book is not None else 100.0
    return dict(lane=row + 1, zombie_count=len(zs), nearest_zombie_x=nx,
                nearest_closeness=level, plant_count=len(ps), threat_level=level,
                priority=round(priority, 2), zombie_strength=round(power, 2),
                shooter_support=round(shooters, 2), reflect_support=round(reflect, 2),
                blocking_walls=walls,
                mower_available=mower, pressure=round(pressure, 2),
                ranged_zombies=ranged, crush_zombies=crush, eat_dps=eat_dps,
                nearest_is_crush=nearest_is_crush)


def saving_plan(board, book):
    """Reserve for a usable upgrade, but release the reserve when defence is urgent."""
    facts = [lane_facts(board, r, book) for r in range(board.rows)]
    producers = sum(book.has_tag(p.type_id, T_PRODUCER) for p in board.plants)
    # Opening option（★ 用户硬约束：女王开局必种；2026-09-26 实战对照后**回滚到
    # 基线语义**）：场上没僵尸就一直攒（不限时钟）；僵尸上草坪后只在开局 30 秒
    # 内且无危急路时继续攒，之后转正常运营先建防线，平静了再回来兑现女王。
    # —— 泳池局实测教训：90s 宽限+只认危急路，让 agent 在 6 只僵尸进场时把
    #    600 阳光全砸进女王、0 防御硬吃第一波。宽限不是越长越好。
    # Runtime prices are authoritative (some versions charge 600 rather than 500).
    # 唯一放弃条件：出现危急路（僵尸贴脸）——保命优先于经济，但仅此一条。
    grace = (board.game_clock or 0) < 3000
    no_critical = not any(f['threat_level'] == 'critical' for f in facts)
    if (producers < 4 and not any(
            book.has_tag(p.type_id,T_TORCH) for p in board.plants)
            and (not board.zombies or (grace and no_critical))):
        opening_grace = grace
        for slot in board.slots:
            cost = book.cost(slot.type_id)
            gap_ok = opening_grace or (board.sun or 0) >= cost-100
            if (slot.ready and book.has_tag(slot.type_id,T_TORCH)
                    and cost is not None and gap_ok
                    and any(any(c in (1,2,3) for c in rear_cols(board,book,r)) for r in range(board.rows))):
                return dict(type_id=slot.type_id,slot=slot.index,plant=book.en(slot.type_id),
                            cost=cost,missing_sun=max(0,cost-(board.sun or 0)),
                            utility=upgrade_value(board,book,slot.type_id),
                            reason='HARD CONSTRAINT (user-confirmed): the opening Sunflower Queen is '
                                   'mandatory. Save while the board is calm; once zombies are on the '
                                   'lawn past the opening grace, build minimum defence first and '
                                   'return to her when calm.')
    # 中段储蓄门槛 4→3（用户 2026-09-26：女王算一株产阳光，3 株就该开始攒
    # 第二个高价值植物增强前期强度，向日葵保持勤奋补种即可）。
    if producers < 3 or not any(f['zombie_count'] for f in facts) or any(
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


def stall_window(board, book, row, type_id, col=None):
    """Conservative planning estimates, NOT measured movement or cooldown seconds."""
    enemies = board.zombies_in_lane(row)
    known = [z for z in enemies if z.x is not None]
    if not known:
        return None
    nearest = min(known,key=lambda z:z.x)
    rear = [p for p in board.plants_in_lane(row)
            if not p.asleep and cell_x(p.col) < nearest.x
            and (col is None or p.col < col)
            and book.has_tag(p.type_id,T_SHOOTER)]
    dps = sum(target_dps(board,book,p.type_id,p.row,p.col,nearest) for p in rear)
    hp = book.hp(type_id)
    if not hp or not dps or nearest.hp is None:
        return None
    # Several nearby mouths shorten a disposable plant's useful life.
    mouths = sum(abs(z.x-nearest.x) <= 80 for z in known)
    # 啃食速度按僵尸类型取最大（黑橄榄球类比普通僵尸快，用户 2026-09-26 报告；
    # zombie_traits.json 未确认的类型回落到 100/s 保守值）。
    eat = max([book.zombie_trait(z.type_id, 'eat_dps') or 100 for z in known] or [100])
    delay = hp / (eat * max(1,mouths))
    kill = (nearest.hp + (nearest.armor_hp or 0)) / dps
    front = max(cell_x(p.col) for p in rear)
    contact = max(0,nearest.x-front-40)/8
    if contact < kill <= contact+delay:
        return dict(delay=round(delay,1),kill=round(kill,1),contact=round(contact,1))
    return None


def relocation_target(board, book, source, *, ready_only=False, rows=None):
    """Choose a real destination before removing a reusable wall. E/F preferred."""
    options=[]
    snow=getattr(board,'snow_cells',None) or {}
    now=time.time()
    # 2026-09-26 夜战教训：非防撞墙绝不能搬进撞车路 —— 冰车读报会把它直接
    # 压扁（回收高坚果 7000+ 血也是一压就没）。只有防撞墙（雷果子/高冰果）
    # 才允许进撞车路当路障。
    src_anti = bool(book.combat(source.type_id).get('crush_hits')
                    or book.combat(source.type_id).get('lethal_hit_burst'))
    for r in (range(board.rows) if rows is None else rows):
        f=lane_facts(board,r,book)
        if f['crush_zombies'] and not src_anti:
            continue
        nx=f['nearest_zombie_x']
        for c in range(board.cols):
            if board.placement_blocked(r,c): continue
            if (r,c)==source.cell or (r,c) in board.top_occupancy(book):
                continue
            if snow.get((r,c),0)>now:
                continue    # 积雪格融化前不能种植（撞车僵尸压过）
            if nx is not None and cell_x(c)>nx+30:
                continue
            if nx is None or nx>=cell_x(4):
                if c<4:continue
            if not board.can_plant(r,c,source.type_id,book):
                if ready_only: continue
                if not board.is_water(r) or board.has_platform(r,c,book):continue
                if not any(s.ready and book.has_tag(s.type_id,'platform')
                           and book.cost(s.type_id) is not None
                           and book.cost(s.type_id)<=(board.sun or 0) for s in board.slots):continue
            options.append((-f['priority'],r!=source.row,abs(c-4),r,c))
    return tuple(min(options)[-2:]) if options else None

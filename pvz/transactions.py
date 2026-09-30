"""Short verified input transactions; never call Jev or collect sun between steps."""
import time
from types import SimpleNamespace
from .geometry import geometry_blocked, record_geometry_failure
from .board import placement_delta
from .tactics import cell_x,relocation_target,crusher_approaching
from .serialize import board_snapshot
from .plants import T_INSTANT, T_GLOBAL_FREEZE, T_SPLASH3

class TransactionStopped(RuntimeError):
    pass

class PlantTransaction:
    def __init__(self,agent,board):
        self.agent=agent;self.board=board;self.steps=[]
        self.identity=(board.pid,board.board,board.level,board.scene,board.rows)
    def _rdx(self,row):
        """屋顶坡度 per-row 修正（与 agent.execute 同源，2026-09-27）。"""
        if getattr(self.board,'scene',None)==4:
            return getattr(self.agent,'_roof_row_dx',{}).get(row,0)
        return 0
    def read(self,predicate=lambda b:True,tries=6):
        previous=self.board.game_clock
        for _ in range(tries):
            time.sleep(.12)
            b=self.agent.reader.read()
            self.last_observed=b
            if not b.ok or (b.pid,b.board,b.level,b.scene,b.rows)!=self.identity:
                raise TransactionStopped('Board changed during transaction')
            if b.game_clock is not None and previous is not None and b.game_clock>previous:
                b.snow_cells = dict(getattr(self.agent,'_snow_cells',None) or self.board.snow_cells or {})
                apply=getattr(self.agent,'apply_placement_evidence',None)
                if apply: apply(b)
                self.board=b
                self.agent.book.sync_field_copies(b.plants)
                if predicate(b):return b
        raise TransactionStopped('Input not confirmed or game clock stopped')
    def cell_valid(self,tid,row,col):
        b=self.board
        snow=getattr(b,'snow_cells',None) or {}
        if snow.get((row,col),0)>time.time():
            raise TransactionStopped('Destination is snow-covered (crushed by the ice-truck); unplantable until it melts')
        if not b.can_plant(row,col,tid,self.agent.book):
            raise TransactionStopped('Destination no longer plantable')
        nx=min((z.x for z in b.zombies_in_lane(row) if z.x is not None),default=9999)
        area_effect = any(self.agent.book.has_tag(tid, tag)
                          for tag in (T_INSTANT, T_GLOBAL_FREEZE, T_SPLASH3))
        if area_effect:
            # A blast can reach enemies behind its planted cell. Share policy's
            # actual coverage check instead of applying a wall's intercept rule.
            from .policy import Candidate, burst_targets
            if not burst_targets(Candidate('', 'plant', row, col, type_id=tid),
                                 b, self.agent.book):
                raise TransactionStopped('No targets remain in effect range')
        elif cell_x(col)>nx+40:
            raise TransactionStopped('Zombie has passed destination')
        self.agent.layout.configure_board(b)
        if geometry_blocked(self.agent, b, row, col):
            raise TransactionStopped('Destination has a confirmed geometry fault')
        x,y=self.agent.layout.cell_center(row,col)
        if not (0<=x<self.agent.layout.client_w and 0<=y<self.agent.layout.client_h):
            raise TransactionStopped('Destination outside client')
    def place_held(self,tid,row,col):
        self.cell_valid(tid,row,col)
        before=self.board
        self.agent.clicker.click_grid(row,col,self.agent.layout,'transaction place',dx=self._rdx(row))
        def completed(b):
            placed,other=placement_delta(before,b,tid,row,col)
            if other and not placed:
                record_geometry_failure(self.agent, b, row, col)
                raise TransactionStopped(f'Plant landed elsewhere: {other}')
            return placed
        self.read(completed)
        if hasattr(self.agent,'_action_times'):self.agent._action_times.append(time.time())
        self.steps.append({'step':'plant_confirmed','type_id':tid,'cell':[row,col]})

    def place_recovered(self,tid,row,col,source):
        if self.board.held_cursor!=2 or self.board.held_type!=tid:
            raise TransactionStopped('Recovered seed no longer in hand')
        try:
            profile=self.agent.book.combat(tid)
            if not (profile.get('crush_hits') or profile.get('lethal_hit_burst')) and any(
                    self.agent.book.zombie_flag(z.type_id,'crush')
                    for z in self.board.zombies_in_lane(row)):
                raise TransactionStopped('Confirmed crusher entered recovery destination lane')
            self.cell_valid(tid,row,col)
        except TransactionStopped as exc:
            if geometry_blocked(self.agent, self.board, row, col): raise
            destination=relocation_target(self.board,self.agent.book,source,ready_only=True)
            if destination is None: raise
            self.steps.append({'step':'recovery_retargeted','from':[row,col],
                               'to':list(destination),'reason':str(exc),
                               'clock':self.board.game_clock})
            row,col=destination
        self.place_held(tid,row,col)
    def bank(self,tid,row,col):
        self.read();self.cell_valid(tid,row,col)
        slot=next((s for s in self.board.slots if s.type_id==tid and s.ready),None)
        cost=self.agent.book.cost(tid)
        if slot is None or cost is None or cost>(self.board.sun or 0):
            raise TransactionStopped('Required card is not ready or affordable')
        self.agent.clicker.cancel_seed('transaction reset')
        self.read(lambda b:not b.holding)
        self.agent.clicker.click_card(slot.index,self.agent.layout,'transaction pick')
        self.read(lambda b:b.held_cursor==1 and b.held_slot==slot.index and b.held_type==tid)
        self.place_held(tid,row,col)

    def followup(self,tid,row,col):
        self.read()
        try:
            self.cell_valid(tid,row,col)
        except TransactionStopped as exc:
            # Preserve the intended defender and threatened lane. A new pad
            # purchase would break the continuous action and its budget.
            if not self.agent.book.has_tag(tid,'wall') or geometry_blocked(self.agent, self.board, row, col): raise
            source=SimpleNamespace(type_id=tid,cell=(row,col),row=row)
            dest=relocation_target(self.board,self.agent.book,source,ready_only=True,rows=(row,))
            if dest is None: raise
            self.steps.append({'step':'followup_retargeted','from':[row,col],
                               'to':list(dest),'reason':str(exc),'clock':self.board.game_clock})
            row,col=dest
        self.bank(tid,row,col)
    def support(self,tid,row,col):
        if self.board.can_plant(row,col,tid,self.agent.book):return
        if not self.board.platform_required(row):raise TransactionStopped('Blocked land destination')
        slot=next((s for s in self.board.slots if s.ready and self.agent.book.has_tag(s.type_id,'platform')
                   and self.board.platform_fits(row,s.type_id,self.agent.book)),None)
        if slot is None:raise TransactionStopped('Platform (Lily Pad / Flower Pot) unavailable')
        self.bank(slot.type_id,row,col)
    def relocate(self,candidate):
        self.read()
        source = self.shovel_source(candidate)
        if source is None or source.hp is None or source.hp<=800:
            raise TransactionStopped('Source cannot return a viable card')
        urgent=bool(getattr(candidate,'crush_recovery',False))
        if urgent and not crusher_approaching(self.board,self.agent.book,source):
            raise TransactionStopped('Crusher no longer approaching source')
        if urgent:
            self.steps.append({'step':'crush_recovery_started','cell':list(source.cell),
                               'source_hp':source.hp,'expected_returned_hp':source.hp-800,
                               'clock':self.board.game_clock})
        destination=relocation_target(self.board,self.agent.book,source,ready_only=urgent)
        if destination is None and not urgent:raise TransactionStopped('No relocation destination')
        if not urgent:
            # Ordinary relocation can prepare water before taking the seed.
            self.support(source.type_id,*destination)
            self.read();self.cell_valid(source.type_id,*destination)
        source = self.shovel_source(candidate)
        if source is None or source.hp is None or source.hp<=800:
            raise TransactionStopped('Source changed before shovel')
        old_drops={(d.index,d.type_id) for d in self.board.dropped_seeds}
        self.agent.clicker.cancel_seed('pre-recovery reset')
        self.read(lambda b:not b.holding)
        self.agent.clicker.shovel_hotkey()
        try:self.read(lambda b:b.holding_shovel)
        except TransactionStopped:
            # A keyboard miss can fall back to the calibrated button, only on a live board.
            self.read()
            self.agent.clicker.click_shovel(self.agent.layout,'shovel button fallback')
            self.read(lambda b:b.holding_shovel)
        current = self.shovel_source(candidate)
        if current is None or current.hp is None or current.hp<=800:
            raise TransactionStopped('Recovery health window closed before shovel hit')
        if urgent:
            if not crusher_approaching(self.board,self.agent.book,current):
                raise TransactionStopped('Crusher threat cleared before shovel hit')
            # Never delay removal to buy a pad. A vanished destination is okay:
            # retain a verified drop on the lawn instead of planting under a truck.
            destination=relocation_target(self.board,self.agent.book,current,ready_only=True)
        else:
            self.cell_valid(source.type_id,*destination)
        self.agent.layout.configure_board(self.board)
        if geometry_blocked(self.agent, self.board, source.row, source.col):
            raise TransactionStopped('Recovery source has a confirmed geometry fault')
        x,y=self.agent.layout.cell_center(source.row,source.col)
        if not (0<=x<self.agent.layout.client_w and 0<=y<self.agent.layout.client_h):
            raise TransactionStopped('Recovery source outside client')
        removals = getattr(self.agent, '_intentional_removals', set())
        removals.add((source.row, source.col, source.type_id))
        self.agent._intentional_removals = removals
        self.agent.clicker.click_grid(source.row,source.col,self.agent.layout,'recover wall',dx=self._rdx(source.row))
        self.read(lambda b:not any(p.cell==source.cell and p.type_id==source.type_id for p in b.plants))
        if hasattr(self.agent,'_action_times'):self.agent._action_times.append(time.time())
        self.steps.append({'step':'source_removed','cell':list(source.cell)})
        if self.board.held_cursor==2 and self.board.held_type==source.type_id:
            destination=relocation_target(self.board,self.agent.book,source,ready_only=True) if urgent else destination
            if destination is None:raise TransactionStopped('Returned card in hand but no supported destination')
            self.place_recovered(source.type_id,*destination,source);return 'relocated'
        self.agent.clicker.cancel_seed('clear shovel before dropped card')
        self.read(lambda b:not b.holding)
        def find_drop(b):
            return next((d for d in b.dropped_seeds if d.type_id==source.type_id
                         and (d.index,d.type_id) not in old_drops),None)
        self.read(lambda b:find_drop(b) is not None,tries=20)
        drop=find_drop(self.board)
        if urgent:
            destination=relocation_target(self.board,self.agent.book,source,ready_only=True)
            if destination is None:
                self.steps.append({'step':'recovered_card_parked','drop_index':drop.index,
                                   'type_id':drop.type_id,'source_hp_before_shovel':current.hp,
                                   'clock':self.board.game_clock})
                return 'recovered_for_later'
        x,y=self.agent.layout.dropped_seed_center(drop)
        if not (0<=x<self.agent.layout.client_w and 0<=y<self.agent.layout.client_h):
            raise TransactionStopped('Dropped card outside client')
        self.agent.clicker.click_client(x,y,'pick recovered seed')
        self.read(lambda b:b.held_cursor==2 and b.held_type==source.type_id)
        self.steps.append({'step':'recovered_seed_in_hand','type_id':source.type_id})
        self.place_recovered(source.type_id,*destination,source)
        return 'relocated'

    def pick_drop(self,candidate):
        """拾回草坪上的掉落卡（2026-09-26 新增）：回收事务被对局中断时，
        已铲掉的墙会以卡片形式躺在草坪上 —— 没有这一步它就一直丢在那
        （夜战实测两次 source_removed 后中断，7000 血墙 stranded）。"""
        self.read()
        def find_drop():
            return next((d for d in self.board.dropped_seeds if d.type_id==candidate.type_id
                         and (candidate.drop_index is None or d.index==candidate.drop_index)),None)
        drop=find_drop()
        if drop is None:
            raise TransactionStopped('Dropped seed is gone')
        self.agent.clicker.cancel_seed('pre-pick reset')
        self.read(lambda b:not b.holding,tries=14)
        drop=find_drop()
        if drop is None:
            raise TransactionStopped('Dropped seed vanished after reset')
        x,y=self.agent.layout.dropped_seed_center(drop)
        if not (0<=x<self.agent.layout.client_w and 0<=y<self.agent.layout.client_h):
            raise TransactionStopped('Dropped card outside client')
        self.agent.clicker.click_client(x,y,'pick dropped seed')
        self.read(lambda b:b.held_cursor==2 and b.held_type==candidate.type_id,tries=14)
        self.steps.append({'step':'dropped_seed_in_hand','type_id':candidate.type_id})
        # 落点：优先 relocation_target（撞车路/积雪/占用都已排除）；
        # 没有就回原格（卡片躺着的格子本来就是合法位置之一）。
        pseudo=SimpleNamespace(cell=(candidate.row,candidate.col),
                               type_id=candidate.type_id,row=candidate.row,col=candidate.col)
        dest=relocation_target(self.board,self.agent.book,pseudo,ready_only=True)
        if dest is None:raise TransactionStopped('No supported destination for held seed')
        self.place_recovered(candidate.type_id,*dest,pseudo)

    def shovel_source(self, candidate):
        source = self.board.shovel_target(candidate.row, candidate.col, self.agent.book)
        if (source is None or source.type_id != candidate.type_id or
                (getattr(candidate, 'source_index', None) is not None and
                 source.index != candidate.source_index)):
            raise TransactionStopped('Shovel source layer or identity changed')
        return source

    def salvage(self,candidate):
        """铲掉换阳光（用户高级技巧 2026-09-26）：植物快被啃死/即将被压扁时，
        铲掉它把损失变成阳光。与 relocate 不同——不期待掉落卡片，只确认
        植物消失，并记录铲前铲后的阳光差供后续校准返还量。"""
        self.read()
        source = self.shovel_source(candidate)
        if source is None:
            raise TransactionStopped('Nothing left to salvage')
        replacement = getattr(candidate,'replacement_type',None)
        def replacement_valid():
            if replacement is None: return
            from .policy import action_invalid_reason
            reason = action_invalid_reason(candidate,self.board,self.agent.book)
            if reason: raise TransactionStopped(reason)
        replacement_valid()
        support_ids = {(p.index,p.type_id) for p in self.board.plants if p.cell==source.cell
                       and self.board.placement_layer(p.type_id,self.agent.book)=='platform'}
        sun0=self.board.sun
        self.agent.clicker.cancel_seed('pre-salvage reset')
        self.read(lambda b:not b.holding)
        self.agent.clicker.shovel_hotkey()
        try:self.read(lambda b:b.holding_shovel)
        except TransactionStopped:
            # Keyboard miss can fall back to the calibrated button, only on a live board.
            self.read()
            self.agent.clicker.click_shovel(self.agent.layout,'shovel button fallback')
            self.read(lambda b:b.holding_shovel)
        current = self.shovel_source(candidate)
        if current is None:
            raise TransactionStopped('Plant gone before shovel hit')
        replacement_valid()
        removals = getattr(self.agent, '_intentional_removals', set())
        removals.add((source.row, source.col, source.type_id))
        self.agent._intentional_removals = removals
        self.agent.clicker.click_grid(source.row,source.col,self.agent.layout,'salvage shovel',dx=self._rdx(source.row))
        self.read(lambda b:not any(p.cell==source.cell and p.type_id==source.type_id
                                   for p in b.plants))
        if hasattr(self.agent,'_action_times'):self.agent._action_times.append(time.time())
        self.steps.append({'step':'salvaged','cell':list(source.cell),
                           'sun_before':sun0,'sun_after':self.board.sun})
        if self.board.holding:
            # 少数植物铲掉可能附带掉卡；捡不起来就复位光标，交给用户/后续轮次。
            self.agent.clicker.cancel_seed('post-salvage reset')
            self.read(lambda b:not b.holding)
        if replacement is not None:
            if not support_ids.issubset({(p.index,p.type_id) for p in self.board.plants if p.cell==source.cell}):
                raise TransactionStopped('Supporting platform changed during replacement')
            self.bank(replacement,source.row,source.col)
            self.steps.append({'step':'replacement_confirmed','type_id':replacement,'cell':list(source.cell)})
        # 返还的阳光是**落在草坪上的拾取物**（夜战实测 9 次里 5 次即时读数为 0，
        # 有一次 +100 其实是天上掉的）—— 解除该区域的假阳性抑制，让收阳光
        # 流程能把这份返还捡回来，而不是被之前的误点记录压着烂掉。
        forgive=getattr(self.agent,'sun_tracker',None)
        if forgive is not None:
            forgive.forgive_cell(source.row,source.col,self.agent.layout)


def run_transaction(agent,board,*,candidate=None,followup=None,pick=None):
    tx=PlantTransaction(agent,board)
    try:
        if candidate is not None:
            if getattr(candidate,'salvage',False):
                tx.salvage(candidate)
                return {'kind':'replaced' if getattr(candidate,'replacement_type',None) is not None else 'salvaged',
                        'completed':True,'steps':tx.steps}
            kind=tx.relocate(candidate)
            return {'kind':kind,'completed':True,'steps':tx.steps}
        if pick is not None:
            tx.pick_drop(pick)
            return {'kind':'pick_drop','completed':True,'steps':tx.steps}
        tx.followup(*followup)
        return {'kind':'support_followup','completed':True,'steps':tx.steps}
    except TransactionStopped as exc:
        # Never let later sun collection place a held plant or use a held shovel.
        last=getattr(tx,'last_observed',tx.board)
        if last.ok and (last.pid,last.board,last.level,last.scene,last.rows)==tx.identity:
            agent.clicker.cancel_seed('transaction stopped')
        return {'kind':'transaction_incomplete','completed':False,'steps':tx.steps,
                'note':str(exc),'last_board':board_snapshot(last)}

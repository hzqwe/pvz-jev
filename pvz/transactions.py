"""Short verified input transactions; never call Jev or collect sun between steps."""
import time
from .board import placement_delta
from .tactics import cell_x,relocation_target

class TransactionStopped(RuntimeError):
    pass

class PlantTransaction:
    def __init__(self,agent,board):
        self.agent=agent;self.board=board;self.steps=[]
        self.identity=(board.pid,board.board,board.scene,board.rows)
    def read(self,predicate=lambda b:True):
        previous=self.board.game_clock
        for _ in range(6):
            time.sleep(.12)
            b=self.agent.reader.read()
            if not b.ok or (b.pid,b.board,b.scene,b.rows)!=self.identity:
                raise TransactionStopped('Board changed during transaction')
            if b.game_clock is not None and previous is not None and b.game_clock>previous:
                self.board=b
                self.agent.book.sync_field_copies(b.plants)
                if predicate(b):return b
        raise TransactionStopped('Input not confirmed or game clock stopped')
    def cell_valid(self,tid,row,col):
        b=self.board
        if not b.can_plant(row,col,tid,self.agent.book):
            raise TransactionStopped('Destination no longer plantable')
        nx=min((z.x for z in b.zombies_in_lane(row) if z.x is not None),default=9999)
        if cell_x(col)>nx+40:
            raise TransactionStopped('Zombie has passed destination')
        self.agent.layout.configure_board(b)
        x,y=self.agent.layout.cell_center(row,col)
        if not (0<=x<self.agent.layout.client_w and 0<=y<self.agent.layout.client_h):
            raise TransactionStopped('Destination outside client')
    def place_held(self,tid,row,col):
        self.cell_valid(tid,row,col)
        before=self.board
        self.agent.clicker.click_grid(row,col,self.agent.layout,'transaction place')
        def completed(b):
            placed,other=placement_delta(before,b,tid,row,col)
            if other and not placed:
                lay=self.agent.layout
                self.agent._geometry_error=(b.scene,b.rows,lay.client_w,lay.client_h,
                    lay.grid_left,lay.grid_top,lay.cell_w,lay.row_height())
                raise TransactionStopped(f'Plant landed elsewhere: {other}')
            return placed
        self.read(completed)
        if hasattr(self.agent,'_action_times'):self.agent._action_times.append(time.time())
        self.steps.append({'step':'plant_confirmed','type_id':tid,'cell':[row,col]})
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
    def support(self,tid,row,col):
        if self.board.can_plant(row,col,tid,self.agent.book):return
        if not self.board.is_water(row):raise TransactionStopped('Blocked land destination')
        slot=next((s for s in self.board.slots if s.ready and self.agent.book.has_tag(s.type_id,'platform')),None)
        if slot is None:raise TransactionStopped('Lily Pad unavailable')
        self.bank(slot.type_id,row,col)
    def relocate(self,candidate):
        self.read()
        source=next((p for p in self.board.plants if p.cell==(candidate.row,candidate.col)
                     and p.type_id==candidate.type_id),None)
        if source is None or source.hp is None or source.hp<=800:
            raise TransactionStopped('Source cannot return a viable card')
        destination=relocation_target(self.board,self.agent.book,source)
        if destination is None:raise TransactionStopped('No relocation destination')
        row,col=destination
        # Prepare water first: never leave a recovered seed in hand while selecting a pad.
        self.support(source.type_id,row,col)
        self.read();self.cell_valid(source.type_id,row,col)
        source=next((p for p in self.board.plants if p.cell==source.cell and p.type_id==source.type_id),None)
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
        current=next((p for p in self.board.plants if p.cell==source.cell and p.type_id==source.type_id),None)
        if current is None or current.hp is None or current.hp<=800:
            raise TransactionStopped('Recovery health window closed before shovel hit')
        self.cell_valid(source.type_id,row,col)
        self.agent.clicker.click_grid(source.row,source.col,self.agent.layout,'recover wall')
        self.read(lambda b:not any(p.cell==source.cell and p.type_id==source.type_id for p in b.plants))
        if hasattr(self.agent,'_action_times'):self.agent._action_times.append(time.time())
        self.steps.append({'step':'source_removed','cell':list(source.cell)})
        if self.board.held_cursor==2 and self.board.held_type==source.type_id:
            self.place_held(source.type_id,row,col);return
        self.agent.clicker.cancel_seed('clear shovel before dropped card')
        self.read(lambda b:not b.holding)
        def find_drop(b):
            return next((d for d in b.dropped_seeds if d.type_id==source.type_id
                         and (d.index,d.type_id) not in old_drops),None)
        self.read(lambda b:find_drop(b) is not None)
        drop=find_drop(self.board)
        x,y=self.agent.layout.dropped_seed_center(drop)
        if not (0<=x<self.agent.layout.client_w and 0<=y<self.agent.layout.client_h):
            raise TransactionStopped('Dropped card outside client')
        self.agent.clicker.click_client(x,y,'pick recovered seed')
        self.read(lambda b:b.held_cursor==2 and b.held_type==source.type_id)
        self.steps.append({'step':'recovered_seed_in_hand','type_id':source.type_id})
        self.place_held(source.type_id,row,col)


def run_transaction(agent,board,*,candidate=None,followup=None):
    tx=PlantTransaction(agent,board)
    try:
        if candidate is not None:tx.relocate(candidate)
        else:tx.bank(*followup)
        return {'kind':'relocated' if candidate is not None else 'support_followup',
                'completed':True,'steps':tx.steps}
    except TransactionStopped as exc:
        # Never let later sun collection place a held plant or use a held shovel.
        agent.clicker.cancel_seed('transaction stopped')
        return {'kind':'transaction_incomplete','completed':False,'steps':tx.steps,'note':str(exc)}

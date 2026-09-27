"""Exercise complete transactions with a simulated game, no real input or API."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pvz.board import BoardState,Plant,SeedSlot,DroppedSeed,Zombie
from pvz.plants import PlantBook
from pvz.policy import Candidate
from pvz.ui import Layout
from pvz.transactions import run_transaction

PAD,WALL=16,161
class FakeGame:
    def __init__(self):
        self.state=BoardState(ok=True,ui=3,scene=2,rows=6,game_clock=1,sun=100,
            plants=[Plant(0,0,1,WALL,hp=8000)],slots=[SeedSlot(0,PAD,0,100)])
        self.actions=[];self.frozen=False;self.miss=False;self.cross=False
    def read(self):
        if not self.frozen:self.state.game_clock+=1
        return copy.deepcopy(self.state)
    def cancel_seed(self,*args):
        self.actions.append('cancel');self.state.holding=False;self.state.held_cursor=0
    def shovel_hotkey(self):
        self.actions.append('key1');self.state.holding=True;self.state.held_cursor=6
        if self.cross:self.state.zombies=[Zombie(0,2,0,x=100)]
    def click_shovel(self,*args):self.shovel_hotkey()
    def click_card(self,index,*args):
        self.actions.append('pick_pad');self.state.holding=True;self.state.held_cursor=1
        self.state.held_slot=index;self.state.held_type=PAD
    def click_grid(self,row,col,*args):
        s=self.state
        if s.held_cursor==6:
            self.actions.append('shovel')
            s.plants=[p for p in s.plants if p.cell!=(row,col)]
            s.dropped_seeds=[DroppedSeed(0,WALL,100,150,50,70)]
        else:
            tid=s.held_type;self.actions.append('place_pad' if tid==PAD else 'place_wall')
            s.plants.append(Plant(10+len(s.plants),row+(1 if self.miss else 0),col,tid,hp=7200))
            if tid==PAD:s.sun-=25
            else:s.dropped_seeds=[]
        s.held_cursor=0;s.holding=False
    def click_client(self,*args):
        self.actions.append('pick_drop');self.state.holding=True
        self.state.held_cursor=2;self.state.held_type=WALL

class TransactionTests(unittest.TestCase):
    def agent(self,g):
        book=PlantBook(hybrid_file='',cost_file='',ids_file='')
        book.bind_one(PAD,'睡莲');book.bind_one(WALL,'回收高坚果')
        return SimpleNamespace(reader=g,clicker=g,book=book,layout=Layout(),_geometry_error=None)
    def test_water_recovery_is_complete_and_card_is_reused(self):
        g=FakeGame();g.state.zombies=[Zombie(0,2,0,x=650,hp=1000)]
        agent=self.agent(g)
        c=Candidate('x','shovel',0,1,type_id=WALL,relocate_to=(2,4))
        with patch('pvz.transactions.time.sleep'):
            result=run_transaction(agent,g.read(),candidate=c)
        self.assertTrue(result['completed'],result)
        self.assertLess(g.actions.index('place_pad'),g.actions.index('shovel'))
        self.assertLess(g.actions.index('pick_drop'),g.actions.index('place_wall'))
        self.assertEqual(g.state.sun,75)
        self.assertEqual({p.type_id for p in g.state.plants if p.cell==(2,4)},{PAD,WALL})
        self.assertIn((0,1,WALL),agent._intentional_removals)
    def test_stopped_clock_does_not_shovel(self):
        g=FakeGame();a=self.agent(g);b=g.read();g.frozen=True
        with patch('pvz.transactions.time.sleep'):
            result=run_transaction(a,b,candidate=Candidate('x','shovel',0,1,type_id=WALL))
        self.assertFalse(result['completed']);self.assertNotIn('shovel',g.actions)

    def test_recovered_card_retargets_when_zombie_passes_during_pickup(self):
        class MovingGame(FakeGame):
            def click_client(self,*args):
                super().click_client(*args)
                self.state.zombies=[Zombie(0,2,0,x=100,hp=1000)]
        g=MovingGame();g.state.zombies=[Zombie(0,2,0,x=650,hp=1000)]
        with patch('pvz.transactions.time.sleep'):
            result=run_transaction(self.agent(g),g.read(),
                candidate=Candidate('x','shovel',0,1,type_id=WALL))
        self.assertTrue(result['completed'],result)
        self.assertTrue(any(s['step']=='recovery_retargeted' for s in result['steps']))
        wall=next(p for p in g.state.plants if p.type_id==WALL)
        self.assertNotEqual(wall.cell,(2,4))
        self.assertFalse(wall.row in (2,3))
        self.assertNotIn('pick_pad',g.actions[g.actions.index('pick_drop'):])

    def test_failed_transaction_keeps_last_cursor_evidence(self):
        g=FakeGame();g.frozen=True
        with patch('pvz.transactions.time.sleep'):
            result=run_transaction(self.agent(g),g.read(),
                candidate=Candidate('x','shovel',0,1,type_id=WALL))
        self.assertIn('last_board',result)
        self.assertIn('cursor',result['last_board'])

    def test_pad_followup_retargets_same_lane_before_buying_defender(self):
        class BankGame(FakeGame):
            def click_card(self,index,*args):
                super().click_card(index,*args)
                self.state.held_type=self.state.slots[index].type_id
        g=BankGame();g.state.sun=500
        g.state.plants=[Plant(1,2,2,PAD),Plant(2,2,4,PAD)]
        g.state.slots=[SeedSlot(0,WALL,0,1000)]
        g.state.zombies=[Zombie(1,2,0,x=300,hp=1000)]
        with patch('pvz.transactions.time.sleep'):
            result=run_transaction(self.agent(g),g.read(),followup=(WALL,2,4))
        self.assertTrue(result['completed'],result)
        self.assertTrue(any(s['step']=='followup_retargeted' for s in result['steps']))
        self.assertTrue(any(p.type_id==WALL and p.cell==(2,2) for p in g.state.plants))
        self.assertEqual(len([p for p in g.state.plants if p.type_id==PAD]),2)
    def test_zombie_passes_destination_before_shovel_keeps_source(self):
        g=FakeGame();g.cross=True;g.state.zombies=[Zombie(0,2,0,x=650)]
        with patch('pvz.transactions.time.sleep'):
            result=run_transaction(self.agent(g),g.read(),candidate=Candidate('x','shovel',0,1,type_id=WALL))
        self.assertFalse(result['completed']);self.assertNotIn('shovel',g.actions)
    def test_wrong_pad_cell_stops_before_shovel(self):
        g=FakeGame();g.miss=True;g.state.zombies=[Zombie(0,2,0,x=650)]
        a=self.agent(g)
        with patch('pvz.transactions.time.sleep'):
            result=run_transaction(a,g.read(),candidate=Candidate('x','shovel',0,1,type_id=WALL))
        self.assertFalse(result['completed']);self.assertIsNotNone(a._geometry_error)
        self.assertNotIn('shovel',g.actions)

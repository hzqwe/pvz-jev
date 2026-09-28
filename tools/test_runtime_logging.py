"""Runtime and evidence regressions from the two 00:48 matches; no live input."""
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from pvz.agent import PvZJevAgent,AgentConfig,AgentStats
from pvz.board import BoardState,Plant,Zombie,DroppedSeed
from pvz.jev import DecisionLog
from pvz.policy import Candidate,action_is_current,generate_candidates
import test_strategy as fixtures
from test_strategy import WALL,PEA,SUN
from tools import launch,review_battle
from tools.replay_battle_review import snapshot
from test_review import rec


class RuntimeLoggingTests(unittest.TestCase):
    board=fixtures.StrategyTests.board
    setUp=fixtures.StrategyTests.setUp

    def test_default_launch_runs_without_duration_cap(self):
        cfg=next(c for key,_,c in launch.MENU if key==launch.DEFAULT_KEY)
        self.assertEqual(cfg['duration'],0)

    def test_zero_duration_passes_infinite_deadline(self):
        a=PvZJevAgent.__new__(PvZJevAgent)
        a.cfg=AgentConfig(verbose=False);a.stats=AgentStats();a._stop_path='unused'
        a.refresh_window=lambda:False;a._start_watchdog=Mock();a._wd_stop=Mock()
        a._loop=Mock(return_value=a.stats)
        a.run(duration_s=0)
        self.assertTrue(math.isinf(a._loop.call_args.args[0]))

    def test_restarted_game_same_size_rebinds_clicker_window(self):
        from pvz.win32 import WindowInfo
        from pvz.ui import Clicker,Layout
        old=WindowInfo(101,1,'x','old',(0,0,800,600),(0,0,800,600),(800,600),True)
        new=WindowInfo(202,2,'x','new',(0,0,800,600),(0,0,800,600),(800,600),True)
        a=PvZJevAgent.__new__(PvZJevAgent);a.cfg=AgentConfig(verbose=False)
        a.book=self.book
        a.reader=SimpleNamespace(attached=True,pid=2);a.win=old;a.layout=Layout()
        a.clicker=Clicker(old);a._need_focus=False
        with patch('pvz.agent.find_game_window',return_value=new):
            self.assertTrue(a.refresh_window())
        self.assertEqual(a.clicker.win.hwnd,202)

    def test_disappeared_game_gets_grace_then_timeout(self):
        a=PvZJevAgent.__new__(PvZJevAgent);a.cfg=AgentConfig(verbose=False)
        a._seen_pid=123;a._missing_since=None;a._exit_reason=None
        self.assertTrue(callable(getattr(a,'wait_for_missing_game',None)))
        with patch('pvz.agent.time.time',return_value=100):
            self.assertTrue(a.wait_for_missing_game(900))
        with patch('pvz.agent.time.time',return_value=221):
            self.assertFalse(a.wait_for_missing_game(900))
        self.assertEqual(a._exit_reason,'game_missing_timeout')

    def loop_agent(self):
        a=PvZJevAgent.__new__(PvZJevAgent);a.cfg=AgentConfig(verbose=False)
        a.stats=AgentStats();a._stop_path='unused';a._seen_pid=123
        a._stop_requested=Mock(side_effect=[False,True]);a._game_still_there=Mock(return_value=True)
        a.ensure_window=Mock(return_value=True);a._window_miss=0;a._dead_misses=0
        a.reader=SimpleNamespace(read=lambda:BoardState(ok=False,ui=2,reason='select cards'))
        return a

    def test_menus_after_startup_deadline_continue_until_user_stops(self):
        a=self.loop_agent()
        with patch('pvz.agent.time.time',return_value=1000),patch('pvz.agent.time.sleep'):
            a._loop(float('inf'),900,900)
        self.assertEqual(a.stats.cycles,2)
        self.assertEqual(a._exit_reason,'user_stop')

    def test_missing_process_waits_without_any_window_operations(self):
        a=self.loop_agent();a._game_still_there.return_value=False
        with patch('pvz.agent.time.time',return_value=100),patch('pvz.agent.time.sleep'):
            a._loop(float('inf'),900,900)
        a.ensure_window.assert_not_called()
        self.assertEqual(a.stats.cycles,2)

    def test_events_do_not_pollute_decision_file(self):
        with tempfile.TemporaryDirectory() as folder:
            log=DecisionLog(str(Path(folder)/'decisions.jsonl'))
            self.assertTrue(callable(getattr(log,'event',None)))
            log.append({'record_type':'decision','battle_id':'b1'})
            log.event('session_end',run_id='r1',reason='user_stop')
            self.assertEqual(len(log.read_all()),1)
            self.assertIn('session_end',Path(log.event_path).read_text(encoding='utf-8'))

    def test_snapshot_preserves_precise_enemy_position_and_cursor(self):
        from pvz import serialize
        self.assertTrue(callable(getattr(serialize,'board_snapshot',None)))
        b=self.board([],[Zombie(37,0,7,x=329.610412,hp=270,armor_hp=1200)])
        b.held_cursor=2;b.held_type=WALL
        s=serialize.board_snapshot(b)
        self.assertEqual(s['zombies'][0]['index'],37)
        self.assertEqual(s['zombies'][0]['x'],329.610412)
        self.assertEqual(s['cursor']['type_id'],WALL)

    def test_new_snapshot_replay_preserves_entity_identity_and_cooldown(self):
        from pvz.serialize import board_snapshot
        b=self.board([PEA],[Zombie(37,0,7,x=329.610412,hp=270,armor_hp=1200)])
        b.slots[0].cd_left=77;b.held_cursor=2;b.held_type=WALL;b.holding=True
        b.dropped_seeds=[DroppedSeed(42,WALL,202.25,150,50,70)]
        copy=snapshot({'board':board_snapshot(b)},self.book)
        self.assertEqual(copy.zombies[0].x,329.610412)
        self.assertEqual(copy.zombies[0].index,37)
        self.assertEqual(copy.slots[0].cd_left,77)
        self.assertEqual(copy.dropped_seeds[0].index,42)
        self.assertTrue(copy.holding)

    def test_unconfirmed_failure_does_not_block_every_plant(self):
        a=PvZJevAgent.__new__(PvZJevAgent);a._blocked_cells={};a._bad_cells={}
        self.assertTrue(callable(getattr(a,'note_placement_failure',None)))
        c=Candidate('x','plant',0,4,type_id=WALL)
        b=self.board([WALL]);b.holding=False
        a.note_placement_failure(c,b)
        self.assertFalse(a._blocked_cells)

    def test_confirmed_rejected_cell_blocks_different_card(self):
        a=PvZJevAgent.__new__(PvZJevAgent);a._blocked_cells={};a._bad_cells={}
        self.assertTrue(callable(getattr(a,'note_placement_failure',None)))
        b=self.board([WALL,PEA],sun=500);b.holding=True;b.held_type=WALL;b.held_cursor=1
        a.note_placement_failure(Candidate('x','plant',0,4,type_id=WALL),b)
        b.rejected_cells=dict(a._blocked_cells)
        self.assertFalse(b.can_plant(0,4,PEA,self.book))
        b.rejected_cells[(0,4)]=0
        self.assertTrue(b.can_plant(0,4,PEA,self.book))

    def test_recovery_source_is_valid_without_shortlist_membership(self):
        self.book.bind_one(161,'回收高坚果')
        b=self.board([],plants=[Plant(1,0,1,161,hp=8000)])
        c=Candidate('x','shovel',0,1,type_id=161,relocate_to=(0,4))
        with patch('pvz.policy.generate_candidates',return_value=[Candidate('wait','wait')]):
            self.assertTrue(action_is_current(c,b,self.book))

    def test_queen_combat_fallback_at_column_e_remains_valid(self):
        self.book.bind_one(86,'向日葵女王')
        b=self.board([86],[Zombie(0,0,0,x=600)],
                     plants=[Plant(i,0,i,PEA) for i in range(4)]
                     +[Plant(10,0,5,WALL,hp=4000)]
                     +[Plant(10+r,r,0,WALL,hp=4000) for r in range(1,5)])
        c=next(c for c in generate_candidates(b,self.book)
               if c.type_id==86 and c.row==0 and c.col==4)
        self.assertTrue(action_is_current(c,b,self.book))

    def test_wall_resuming_contact_is_not_repositioned_at_full_health(self):
        self.book.bind_one(161,'回收高坚果')
        b=self.board([],[Zombie(1,0,0,x=180)],plants=[Plant(1,0,1,161,hp=8000)])
        c=Candidate('x','shovel',0,1,type_id=161,relocate_to=(0,4))
        self.assertFalse(action_is_current(c,b,self.book))

    def test_pick_source_follows_drop_entity_after_it_moves(self):
        self.book.bind_one(161,'回收高坚果')
        b=self.board([]);b.dropped_seeds=[DroppedSeed(4,161,200,150,50,70)]
        cs=generate_candidates(b,self.book);c=next(c for c in cs if c.kind=='pick')
        b.dropped_seeds[0].x=230
        with patch('pvz.policy.generate_candidates',return_value=[]):
            self.assertTrue(action_is_current(c,b,self.book))

    def test_review_detects_real_nested_critical_lane(self):
        r=rec(10);r['state']['lanes'][0]['tactical_assessment']={'threat_level':'critical'}
        self.assertIn('危急时仍在等待',{f['kind'] for f in review_battle.detect([r])})

    def test_switching_games_is_not_a_cadence_stall(self):
        a=rec(10,clock=44000);b=rec(100,clock=86)
        self.assertNotIn('决策节奏空档',{f['kind'] for f in review_battle.detect([a,b])})

    def test_review_groups_old_and_new_battle_formats(self):
        self.assertTrue(callable(getattr(review_battle,'split_battles',None)))
        a=rec(1,clock=44000);b=rec(2,clock=86)
        self.assertEqual(len(review_battle.split_battles([a,b])),2)
        a['battle_id']='r-b1';b['battle_id']='r-b2';b['state']['game']['clock']=45000
        self.assertEqual(len(review_battle.split_battles([a,b])),2)

    def test_opening_clock_is_ticks_not_seconds(self):
        records=[rec(10+i,sun=50,clock=3000,producers=0) for i in range(6)]
        self.assertNotIn('经济崩盘（中盘赤字）',{f['kind'] for f in review_battle.detect(records)})

    def test_low_reserve_with_healthy_income_is_not_economic_collapse(self):
        records=[rec(10+i,sun=50,clock=20000,producers=3) for i in range(6)]
        self.assertNotIn('经济崩盘（中盘赤字）',{f['kind'] for f in review_battle.detect(records)})

    def test_review_counts_shovel_inside_interrupted_recovery(self):
        r=rec(1,executed={'kind':'transaction_incomplete','completed':False,
            'steps':[{'step':'source_removed','cell':[0,1]}]})
        self.assertEqual(review_battle.kpis([r])['shovels'],1)

    def test_review_detects_different_cards_rejected_at_one_cell(self):
        rs=[rec(10+i*4,executed={'kind':'click','placed':False,'grid':'r0c4','plant':name})
            for i,name in enumerate(('Wall','IceWall'))]
        self.assertIn('同格换卡仍失败',{f['kind'] for f in review_battle.detect(rs)})

if __name__=='__main__':unittest.main()

"""Regressions from the September 27/28 battle logs; no game input or API."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
import test_strategy as fixtures
from pvz.agent import PvZJevAgent
from pvz.board import BoardState, Plant, Zombie
from pvz.policy import Candidate, generate_candidates, merge_decision, build_questions, action_is_current, adapt_stale_candidate
from pvz.jev import JevAnswer, JevResponse

SUN, PEA, WALL, STRONG, BOMB = fixtures.SUN, fixtures.PEA, fixtures.WALL, fixtures.STRONG, fixtures.BOMB
PAD = 414

class BattleReviewFixTests(unittest.TestCase):
    board = fixtures.StrategyTests.board
    setUp = fixtures.StrategyTests.setUp

    def wait(self, b):
        cs = generate_candidates(b, self.book)
        w = next(c for c in cs if c.kind == 'wait')
        resp = JevResponse(answers={'action': JevAnswer('action','choice',{'choice':w.cid}),
                                   'hold_sun': JevAnswer('hold_sun','noul',{'noul':.99})})
        return merge_decision(resp, cs, b, self.book)

    def test_own_shovel_cannot_teach_crush(self):
        p = Plant(0,0,5,WALL,hp=3220)
        prev = self.board([], plants=[p]); prev.game_clock=1000
        cur = self.board([], [Zombie(0,0,7,x=532)], sun=100)
        cur.game_clock=1350; cur.intentional_removals={(0,5,WALL)}
        PvZJevAgent.learn_crush_events(prev,cur,self.book)
        self.assertFalse(self.book.zombie_flag(7,'crush'))

    def test_single_unexplained_loss_is_only_suspicion(self):
        prev = self.board([], plants=[Plant(0,0,5,WALL,hp=4000)])
        cur = self.board([], [Zombie(0,0,9,x=480)])
        prev.game_clock=1000;cur.game_clock=1350
        PvZJevAgent.learn_crush_events(prev,cur,self.book)
        self.assertFalse(self.book.zombie_flag(9,'crush'))

    def test_long_gap_normal_eating_is_not_crush(self):
        prev=self.board([],plants=[Plant(0,0,3,WALL,hp=1000)])
        cur=self.board([],[Zombie(0,0,7,x=300)])
        prev.game_clock=1000;cur.game_clock=2200
        self.assertFalse(PvZJevAgent.learn_crush_events(prev,cur,self.book))

    def test_crush_does_not_imply_ice_trail(self):
        self.book.runtime_crush.add(7)
        agent=SimpleNamespace(book=self.book,_snow_cells={},_zombie_track={(0,7):[(1000,589),(1350,532)]})
        b=self.board([],[Zombie(0,0,7,x=532)])
        PvZJevAgent._track_snow_cells(agent,b)
        self.assertFalse(b.snow_cells)

    def test_unconfirmed_static_crush_is_not_hard_fact(self):
        self.assertFalse(self.book.zombie_flag(12,'crush'))
        self.assertTrue(self.book.zombie_flag(5,'crush'))

    def test_restart_clears_learning_before_observation(self):
        agent=PvZJevAgent.__new__(PvZJevAgent)
        agent.book=self.book;agent._terrain_context=(None,0,0,5)
        agent._last_decision_clock=5000;agent._bad_cells={};agent._zombie_track={};agent._snow_cells={}
        self.book.runtime_crush.add(7)
        self.assertTrue(callable(getattr(agent,'reset_battle_context',None)))
        b=self.board([]);b.scene=0;b.game_clock=100
        agent.reset_battle_context(b)
        self.assertFalse(self.book.runtime_crush)

    def test_first_snapshot_initializes_lazy_trackers(self):
        agent = PvZJevAgent.__new__(PvZJevAgent)
        agent.book = self.book; agent._bad_cells = {}
        agent.reset_battle_context(self.board([]))
        self.assertEqual(agent._zombie_track, {})
        self.assertEqual(agent._snow_cells, {})

    def test_reason_survives_long_almanac(self):
        c=Candidate('A1','plant',0,2,0,STRONG,why='Protect lane 1 before contact',tags=self.book.tags(STRONG))
        text=c.describe(self.book)
        self.assertIn('Reason:',text)
        self.assertIn('Protect lane 1',text)
        self.assertLessEqual(len(text),400)

    def test_pad_exposes_defender_and_total_price(self):
        self.book.bind_one(PAD,'睡莲')
        c=Candidate('A1','plant',2,4,0,PAD,why='Block lane 3 immediately',supports_type=WALL)
        text=c.describe(self.book)
        self.assertIn('Ice Wall-nut',text)
        self.assertIn('175',text)
        self.assertIn('Reason:',text)

    def test_salvage_never_promises_returned_card(self):
        c=Candidate('A1','shovel',0,2,type_id=SUN,salvage=True,why='Recover sun',hp=300)
        self.assertNotIn('seed card',c.describe(self.book))
        self.assertIn('sun',c.describe(self.book))

    def test_dying_pad_is_not_house_rescue(self):
        self.book.bind_one(PAD,'睡莲')
        b=self.board([WALL], [Zombie(0,0,0,x=110,hp=270),Zombie(1,2,0,x=480)],
                     [Plant(0,2,5,PAD,hp=108)],sun=300)
        cs=generate_candidates(b,self.book)
        sh=next(c for c in cs if c.salvage)
        self.assertFalse(sh.emergency)
        resp=JevResponse(answers={'action':JevAnswer('action','choice',{'choice':sh.cid})})
        d=merge_decision(resp,cs,b,self.book)
        self.assertEqual(d.candidate.type_id,WALL)
        self.assertEqual(d.candidate.row,0)

    def test_safety_net_rebuilds_economy_with_180_sun(self):
        q=410;self.book.bind_one(q,'向日葵女王')
        b=self.board([SUN], [Zombie(0,0,0,x=700,hp=270)],
                     [Plant(0,1,2,q),Plant(1,4,0,SUN)],sun=180)
        self.assertFalse(self.wait(b).hold)

    def test_rank_cap_does_not_make_legal_action_stale(self):
        b=self.board([PEA],[Zombie(0,0,0,x=500)],sun=400)
        b.game_clock=4000
        c=next(c for c in generate_candidates(b,self.book) if c.kind=='plant')
        with patch('pvz.policy.generate_candidates', return_value=[Candidate('WAIT','wait')]):
            self.assertTrue(action_is_current(c,b,self.book))

    def test_stale_compound_cannot_change_defender_silently(self):
        self.book.bind_one(PAD,'睡莲')
        a=Candidate('A1','plant',2,4,0,PAD,supports_type=WALL)
        b=Candidate('A2','plant',2,4,0,PAD,supports_type=PEA)
        from pvz.policy import candidate_key
        self.assertNotEqual(candidate_key(a),candidate_key(b))

    def test_stale_adaptation_preserves_compound_intent(self):
        a=Candidate('A1','plant',2,4,0,PAD,supports_type=WALL)
        b=Candidate('A2','plant',2,5,0,PAD,supports_type=PEA)
        with patch('pvz.policy.generate_candidates',return_value=[b]):
            self.assertIsNone(adapt_stale_candidate(a,self.board([]),self.book))

    def test_generated_speed_bump_survives_execution_validation(self):
        b=self.board([PEA],[Zombie(0,0,0,x=110,hp=270)],sun=180)
        c=next(c for c in generate_candidates(b,self.book) if c.kind=='plant' and c.row==0)
        self.assertTrue(action_is_current(c,b,self.book))

    def test_bomb_without_local_targets_is_stale(self):
        b=self.board([BOMB],[Zombie(0,0,0,x=110)],sun=300)
        c=next(c for c in generate_candidates(b,self.book) if c.kind=='plant')
        b.zombies=[Zombie(1,4,0,x=700)]
        self.assertFalse(action_is_current(c,b,self.book))

    def test_slow_tracking_fire_is_not_immediate_rescue(self):
        b=self.board([STRONG],[Zombie(0,0,0,x=100,hp=3000),Zombie(1,3,0,x=600,hp=3000)],sun=1000)
        self.assertFalse(any(c.emergency for c in generate_candidates(b,self.book) if c.type_id==STRONG))

    def test_delayed_global_freeze_is_not_instant_rescue(self):
        snow=418;self.book.bind_one(snow,'雪花寒冰菇')
        b=self.board([snow],[Zombie(0,0,0,x=27,hp=3000)],sun=1000)
        self.assertFalse(any(c.emergency for c in generate_candidates(b,self.book)))

    def test_wait_refresh_accepts_wall_that_just_finished_cooling(self):
        from pvz.agent import AgentConfig, AgentStats
        from pvz.policy import Decision
        old=self.board([WALL],[Zombie(0,0,0,x=180)],sun=300)
        old.slots[0].cd_left=50
        fresh=self.board([WALL],[Zombie(0,0,0,x=110)],sun=300)
        fresh.game_clock=1350
        a=PvZJevAgent.__new__(PvZJevAgent)
        a.book=self.book;a.cfg=AgentConfig(dry_run=False,verbose=False);a.stats=AgentStats()
        a._frozen_hits=0;a._advances=3;a._action_times=[]
        a.reader=SimpleNamespace(read=lambda:fresh);a.clicker=Mock();a.layout=Mock()
        a.layout.configure_board.side_effect=RuntimeError('verification stop before any input')
        d=Decision(candidate=Candidate('WAIT','wait'),hold=True)
        record={'decision':{'hold':True}}
        with self.assertRaisesRegex(RuntimeError,'verification stop'):
            a.execute(d,record,old)
        self.assertEqual(d.candidate.type_id,WALL)
        self.assertFalse(d.hold)
        self.assertFalse(record['decision']['hold'])
        a.clicker.assert_not_called()

    def test_observation_suspicions_expire(self):
        self.book.crush_evidence[9]=[(1000,(1,0,2,PEA))]
        prev=self.board([]);prev.game_clock=7000
        cur=self.board([]);cur.game_clock=7200
        PvZJevAgent.learn_crush_events(prev,cur,self.book)
        self.assertFalse(self.book.crush_evidence)

    def test_no_speed_bump_after_zombie_passes_leftmost_cell(self):
        b=self.board([PEA,SUN],[Zombie(0,0,0,x=27)],sun=180)
        self.assertFalse(any(c.intercept and c.row==0 for c in generate_candidates(b,self.book)))

    def test_pool_economy_reserve_includes_both_plants(self):
        self.book.bind_one(PAD,'睡莲')
        b=self.board([PAD,SUN],sun=180)
        b.rows=6;b.scene=2;b.row_types={r:2 for r in range(6)}
        d=self.wait(b)
        self.assertFalse(d.hold)
        self.assertEqual(d.candidate.supports_type,SUN)
        self.assertEqual(d.candidate.total_cost(self.book),125)

if __name__=='__main__':unittest.main()

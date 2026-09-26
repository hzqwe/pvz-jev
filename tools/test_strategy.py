"""Deterministic tactical regressions; no game input or API calls."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.plants import PlantBook
from pvz.policy import generate_candidates, merge_decision
from pvz.serialize import build_state
from pvz.jev import JevAnswer, JevResponse

SUN, PEA, WALL, STRONG, BOMB, FREEZE = range(300, 306)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        for tid, name in zip(range(300, 306), ['阳光向日葵', '豌豆射手', '冰冻坚果',
                                             '冰瓜香蒲', '阳光炸弹', '雪花寒冰菇']):
            self.assertTrue(self.book.bind_one(tid, name))

    def board(self, cards, zombies=(), plants=(), sun=1000):
        return BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=1000,
                          slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies))

    def choices(self, b):
        return [c for c in generate_candidates(b, self.book) if c.kind == 'plant']

    def test_shooter_stays_behind_wall_when_rear_cell_occupied(self):
        b = self.board([PEA], [Zombie(0, 0, 0, x=650)],
                       [Plant(0, 0, 2, WALL), Plant(1, 0, 1, SUN)], sun=900)
        cs = self.choices(b)
        self.assertTrue(cs)
        self.assertTrue(all(c.col < 2 for c in cs))

    def test_producer_never_spills_in_front_of_wall(self):
        ps = [Plant(r * 3, r, 1, WALL) for r in range(5)]
        ps += [Plant(r * 3 + 1, r, 0, SUN) for r in range(5)]
        self.assertEqual(self.choices(self.board([SUN], plants=ps)), [])

    def test_hybrid_wall_blocks_before_zombie_not_behind_it(self):
        b = self.board([WALL], [Zombie(0, 0, 0, x=260)],
                       [Plant(0, 0, 0, PEA)])
        cs = self.choices(b)
        self.assertTrue(cs)
        self.assertTrue(all(0 < c.col and 40 + 80*c.col <= 260 for c in cs))

    def test_near_house_single_zombie_beats_far_crowd(self):
        zs = [Zombie(0, 4, 0, x=110)]
        zs += [Zombie(i+1, r, 0, x=700) for i, r in enumerate([0,0,0,1,1,1])]
        self.assertEqual(self.choices(self.board([WALL], zs))[0].row, 4)

    def test_saves_for_strong_card_after_economy_established(self):
        ps = [Plant(r, r, 0, SUN) for r in range(4)] + [Plant(10, 0, 2, PEA)]
        b = self.board([SUN, PEA, STRONG], [Zombie(0, 0, 0, x=720)], ps, sun=400)
        cs = generate_candidates(b, self.book)
        self.assertEqual(merge_decision(None, cs, b, self.book).candidate.kind, 'wait')

    def test_emergency_overrides_model_hold(self):
        b = self.board([BOMB, SUN], [Zombie(0, 4, 0, x=110)], sun=300)
        cs = generate_candidates(b, self.book)
        wait = next(c for c in cs if c.kind == 'wait')
        resp = JevResponse(answers={
            'action': JevAnswer('action', 'choice', {'choice': wait.cid, 'confidence': 1}),
            'hold_sun': JevAnswer('hold_sun', 'noul', {'noul': 0.99})})
        d = merge_decision(resp, cs, b, self.book)
        self.assertFalse(d.hold)
        self.assertEqual(d.candidate.type_id, BOMB)

    def test_global_rescue_allowed_for_single_house_threat(self):
        b = self.board([FREEZE], [Zombie(0, 3, 0, x=100)], sun=250)
        self.assertTrue(self.choices(b))

    def test_stale_card_identity_cannot_execute(self):
        b = self.board([PEA], [Zombie(0, 0, 0, x=650)])
        cs = generate_candidates(b, self.book)
        b.slots[0].type_id = SUN
        d = merge_decision(None, cs, b, self.book)
        self.assertEqual(d.candidate.kind, 'wait')

    def test_zombie_identity_is_not_a_plant_name(self):
        b = self.board([], [Zombie(0, 0, 0, x=600)])
        z = build_state(b, self.book)['lanes'][0]['zombies'][0]
        self.assertNotIn('Peashooter', z['kind'])

    def test_no_mower_lane_gets_priority_at_equal_pressure(self):
        b = self.board([WALL], [Zombie(0, 0, 0, x=400), Zombie(1, 4, 0, x=400)])
        b.mowers = {0: True, 4: False}
        self.assertEqual(self.choices(b)[0].row, 4)

    def test_armor_strength_beats_equal_number_of_weak_zombies(self):
        weak, strong = Zombie(0, 0, 0, x=400), Zombie(1, 4, 2, x=400)
        weak.hp, weak.armor_hp = 270, 0
        strong.hp, strong.armor_hp = 270, 3000
        self.assertEqual(self.choices(self.board([WALL], [weak, strong]))[0].row, 4)

    def test_safe_empty_lane_without_mower_does_not_steal_rescue(self):
        b = self.board([WALL], [Zombie(0, 2, 0, x=150)])
        b.mowers = {0: False, 2: True}
        self.assertEqual(self.choices(b)[0].row, 2)

    def test_same_cell_keeps_distinct_plant_options(self):
        b = self.board([PEA, STRONG], [Zombie(0, 0, 0, x=650)],
                       [Plant(r, r, 0, SUN) for r in range(4)])
        self.assertEqual({c.type_id for c in self.choices(b)}, {PEA, STRONG})

    def test_execute_does_not_click_stale_occupied_cell(self):
        from pvz.agent import PvZJevAgent, AgentConfig, AgentStats
        b = self.board([PEA], [Zombie(0, 0, 0, x=650)])
        cs = generate_candidates(b, self.book)
        decision = merge_decision(None, cs, b, self.book)
        c = decision.candidate
        fresh = self.board([PEA], b.zombies, [Plant(0,c.row,c.col,SUN)])
        fresh.game_clock = 1100
        agent = PvZJevAgent.__new__(PvZJevAgent)
        agent.book, agent.cfg, agent.stats = self.book, AgentConfig(dry_run=False,verbose=False), AgentStats()
        agent._frozen_hits, agent._advances, agent._action_times = 0, 3, []
        agent.layout, agent.clicker = object(), Mock()
        agent.reader = SimpleNamespace(read=lambda:fresh)
        record = {}
        agent.execute(decision, record, b)
        self.assertEqual(record['executed']['kind'], 'stale_action')
        self.assertEqual(agent.clicker.mock_calls, [])

    def test_hybrid_wall_adds_layer_in_front_of_existing_wall(self):
        # 2026-09-26 行为变更（用户实测反馈）：墙系混血的价值在被啃（反伤/亡语），
        # 已有墙时应加在墙的**前方**形成纵深，而不是退到墙后当射手。
        z = Zombie(0,0,0,x=600)
        z.hp, z.armor_hp = 270, 3000
        b = self.board([WALL], [z], [Plant(0,0,4,3)])
        cs = self.choices(b)
        self.assertTrue(cs)
        self.assertTrue(all(c.col > 4 for c in cs))

    def test_sleeping_upgrade_does_not_cancel_saving(self):
        ps = [Plant(r,r,0,SUN) for r in range(4)]
        ps += [Plant(10,0,2,PEA), Plant(11,1,2,STRONG,asleep=True)]
        b = self.board([SUN,PEA,STRONG], [Zombie(0,0,0,x=720)], ps, sun=400)
        self.assertEqual(merge_decision(None,generate_candidates(b,self.book),b,self.book).candidate.kind,'wait')

    def test_ready_saving_target_is_bought_instead_of_waiting_forever(self):
        ps = [Plant(r,r,0,SUN) for r in range(4)] + [Plant(10,0,2,PEA)]
        b = self.board([SUN,PEA,STRONG],[Zombie(0,0,0,x=720)],ps,sun=500)
        cs = generate_candidates(b,self.book)
        wait = next(c for c in cs if c.kind=='wait')
        resp = JevResponse(answers={
            'action':JevAnswer('action','choice',{'choice':wait.cid}),
            'hold_sun':JevAnswer('hold_sun','noul',{'noul':0.99})})
        d = merge_decision(resp,cs,b,self.book)
        self.assertFalse(d.hold)
        self.assertEqual(d.candidate.type_id,STRONG)


class MowerReaderTests(unittest.TestCase):
    def reader(self, count=2):
        from pvz.board import BoardReader
        from pvz import offsets as O
        r = BoardReader.__new__(BoardReader)
        r.notes = []
        r.lawn_app_ptr = lambda:100
        data = {1000+O.OFF_MOWER:2000,1000+O.OFF_MOWER_COUNT_MAX:5,
                1000+O.OFF_MOWER_COUNT:count}
        r.pm = SimpleNamespace(u32=lambda a:data.get(a),i32=lambda a:data.get(a),u8=lambda a:data.get(a))
        def slot(i,row,state=1,dead=0):
            a=2000+i*O.MOWER_STRUCT
            data.update({a:100,a+4:1000,a+O.M_ROW:row,a+O.M_STATE:state,a+O.OFF_MOWER_DEAD:dead})
        return r,slot

    def test_partial_pool_is_unknown_not_four_lost_mowers(self):
        r,slot=self.reader(count=5)
        slot(0,0)
        self.assertEqual(r._read_mowers(1000,5),{})

    def test_triggered_and_dead_mowers_are_not_ready(self):
        r,slot=self.reader(count=2)
        slot(0,0)
        slot(1,1,state=2)
        slot(2,2,dead=1)
        self.assertEqual(r._read_mowers(1000,5),{0:True,1:False,2:False,3:False,4:False})

    def test_invalid_health_pair_remains_unknown(self):
        r,_=self.reader()
        r.pm.i32=lambda a:999 if a==1 else 100
        self.assertIsNone(r._health(1,2))


if __name__ == '__main__':
    unittest.main(verbosity=2)

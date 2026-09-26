"""Plant capabilities must affect decisions, not just decorative descriptions."""
import unittest
import test_strategy as fixtures
from test_strategy import SUN, PEA, WALL, STRONG, BOMB, FREEZE
from pvz.board import Plant, Zombie
from pvz.tactics import saving_plan, lane_facts, upgrade_value
from pvz.policy import generate_candidates, merge_decision, action_is_current
from pvz.serialize import build_state

QUEEN, CORN, GATLING, TALL, THUNDER = range(310,315)
CHILL = 315


class PlantKnowledgeTests(unittest.TestCase):
    board = fixtures.StrategyTests.board
    def setUp(self):
        fixtures.StrategyTests.setUp(self)
        for tid,name in zip(range(310,315),['向日葵女王','玉米卷香蒲','狂野机枪射手','高冰果','雷果子']):
            self.book.bind_one(tid,name)
        self.assertTrue(self.book.bind_one(CHILL,'寒光菇'))

    def mature(self,cards,zombies,sun=1200,extra=()):
        return self.board(cards,zombies,[Plant(r,r,0,SUN) for r in range(4)]+list(extra),sun)

    def test_queen_counts_as_actual_firepower(self):
        b=self.board([], [Zombie(0,0,0,x=650)], [Plant(0,4,1,QUEEN)])
        self.assertGreater(lane_facts(b,0,self.book)['shooter_support'],0)

    def test_multilane_saving_can_choose_ice_over_cheapest_corn(self):
        b=self.mature([CORN,STRONG],[Zombie(i,i,0,x=700) for i in range(4)],sun=400)
        self.assertEqual(saving_plan(b,self.book)['type_id'],STRONG)

    def test_concentrated_attack_values_gatling_above_global_corn(self):
        b=self.mature([CORN,GATLING],[Zombie(i,2,0,x=650+i*15) for i in range(4)],sun=450)
        self.assertEqual(saving_plan(b,self.book)['type_id'],GATLING)

    def test_armored_front_can_save_for_thunder(self):
        z=Zombie(0,2,2,x=650,hp=270,armor_hp=500)
        b=self.mature([THUNDER,CORN],[z],sun=350)
        self.assertEqual(saving_plan(b,self.book)['type_id'],THUNDER)

    def test_queen_still_offered_when_economy_slots_target_reached(self):
        ps=[Plant(r*2+c,r,c,SUN) for r in range(5) for c in range(2)]
        b=self.board([QUEEN],[Zombie(0,2,0,x=650)],ps,sun=1200)
        self.assertIn(QUEEN,{c.type_id for c in generate_candidates(b,self.book)})

    def test_all_available_types_get_representation_before_second_placement(self):
        from pvz.plants import load_lineups
        lineup=load_lineups()[0]
        tids=list(range(400,400+len(lineup['order'])))
        self.book.bind_lineup(tids,lineup['order'])
        b=self.mature(tids,[Zombie(i,4,0,x=150+i*12) for i in range(10)],sun=5000)
        offered={c.type_id for c in generate_candidates(b,self.book)}
        self.assertTrue(set(tids)-{t for t in tids if self.book.role(t)=='platform'} <= offered)

    def test_model_receives_damage_and_restrictions_for_seed_cards(self):
        b=self.board([THUNDER],[])
        info=build_state(b,self.book)['seed_cards'][0]
        self.assertIn('effect',info)
        self.assertIn('combat',info)
        self.assertEqual(info['combat']['armor_multiplier'],2)

    def test_burn_aura_only_credits_zombies_in_aura_reach(self):
        near=self.board([],[Zombie(0,2,0,x=300)],sun=0)
        far=self.board([],[Zombie(0,2,0,x=760)],sun=0)
        self.assertGreater(upgrade_value(near,self.book,QUEEN,2),
                           upgrade_value(far,self.book,QUEEN,2))

    def test_reflect_wall_in_contact_reduces_pressure(self):
        z=Zombie(0,0,0,x=250)          # 雷果子种在 col2 (x=240)，僵尸已啃到墙
        with_wall=self.board([],[z],[Plant(0,0,2,THUNDER)])
        without=self.board([],[z])
        fw=lane_facts(with_wall,0,self.book)
        self.assertGreater(fw['reflect_support'],0)
        self.assertLess(fw['pressure'],lane_facts(without,0,self.book)['pressure'])

    def test_reflect_wall_not_in_contact_counts_nothing(self):
        b=self.board([],[Zombie(0,0,0,x=700)],[Plant(0,0,2,THUNDER)])
        self.assertEqual(lane_facts(b,0,self.book)['reflect_support'],0)

    def test_death_freeze_wall_is_mentioned_to_the_model(self):
        b=self.board([TALL],[Zombie(0,0,0,x=300)])
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==TALL]
        self.assertTrue(cs)
        self.assertIn('death ripple',cs[0].why)

    def test_reclaimable_wall_is_mentioned_to_the_model(self):
        self.book.bind_one(320,'回收高坚果')
        b=self.board([320],[Zombie(0,0,0,x=300)])
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==320]
        self.assertTrue(cs)
        self.assertIn('Reclaimable wall',cs[0].why)

    def test_sun_on_kill_scales_with_victims(self):
        one=self.board([BOMB],[Zombie(0,2,0,x=240)])
        three=self.board([BOMB],[Zombie(0,2,0,x=240),Zombie(1,2,0,x=240),Zombie(2,2,0,x=240)])
        v1=max(c.score for c in generate_candidates(one,self.book)
               if c.kind=='plant' and c.type_id==BOMB)
        v3=max(c.score for c in generate_candidates(three,self.book)
               if c.kind=='plant' and c.type_id==BOMB)
        self.assertGreater(v3,v1)

    def test_chill_shroom_credits_sun_per_frozen(self):
        b=self.board([CHILL],[Zombie(i,i%5,0,x=500) for i in range(10)],sun=550)
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==CHILL]
        self.assertTrue(cs)
        self.assertIn('frozen zombie',cs[0].why)

    def test_reclaim_shovel_offered_when_wall_nearly_dead(self):
        self.book.bind_one(320,'回收高坚果')
        wall=Plant(0,0,2,320,hp=1500)
        b=self.board([],[Zombie(0,0,0,x=250)],[wall])   # x=250 已啃到 col2 的墙
        sh=[c for c in generate_candidates(b,self.book) if c.kind=='shovel']
        self.assertTrue(sh)
        self.assertFalse(sh[0].emergency)   # 没在被啃、x=250 也只是 high
        # 正在被啃且只比回收线高 200 血（=2 秒 @100/s 啃速）：窗口将关，判紧急
        biting=Plant(0,0,2,320,hp=1000,recently_eaten=True)
        b2=self.board([],[Zombie(0,0,0,x=250)],[biting])
        sh2=[c for c in generate_candidates(b2,self.book) if c.kind=='shovel']
        self.assertTrue(sh2 and sh2[0].emergency)
        crit=self.board([],[Zombie(0,0,0,x=110)],[wall])
        sh3=[c for c in generate_candidates(crit,self.book) if c.kind=='shovel']
        self.assertTrue(sh3 and sh3[0].emergency)
        self.assertIn('Reclaim it',sh[0].why)
        self.assertIn('Shovel up',sh[0].describe(self.book))
        self.assertEqual(sh[0].hp,1500)

    def test_no_shovel_when_full_or_unknown_or_too_low_hp(self):
        self.book.bind_one(320,'回收高坚果')
        z=Zombie(0,0,0,x=250)
        for hp in (8000,None,500):
            b=self.board([],[z],[Plant(0,0,2,320,hp=hp)])
            self.assertFalse([c for c in generate_candidates(b,self.book)
                              if c.kind=='shovel'], f'hp={hp} 不应有铲子候选')

    def test_no_shovel_when_no_pressure(self):
        self.book.bind_one(320,'回收高坚果')
        b=self.board([],[],[Plant(0,0,2,320,hp=1500)])  # 没有僵尸
        self.assertFalse([c for c in generate_candidates(b,self.book)
                          if c.kind=='shovel'])

    def test_shovel_survives_merge_and_revalidation(self):
        self.book.bind_one(320,'回收高坚果')
        wall=Plant(0,0,2,320,hp=1500)
        b=self.board([],[Zombie(0,0,0,x=250)],[wall])
        cs=generate_candidates(b,self.book)
        sh=[c for c in cs if c.kind=='shovel'][0]
        self.assertTrue(action_is_current(sh,b,self.book))
        d=merge_decision(None,cs,b,self.book)   # Jev 不可用 -> 兜底应选中紧急铲子
        self.assertEqual(d.candidate.kind,'shovel')

    def test_defender_hp_reaches_the_model(self):
        self.book.bind_one(320,'回收高坚果')
        b=self.board([],[Zombie(0,0,0,x=700)],[Plant(0,0,2,320,hp=1500,recently_eaten=True)])
        info=build_state(b,self.book)['lanes'][0]['defenders'][0]
        self.assertEqual(info['hp'],1500)
        self.assertTrue(info['recently_eaten'])

    def test_playbook_reaches_the_model(self):
        self.book.bind_one(320,'回收高坚果')
        b=self.board([320],[Zombie(0,0,0,x=700)])
        st=build_state(b,self.book)
        info=st['seed_cards'][0]
        self.assertIn('reusable',info['usage'])
        self.assertTrue(st['doctrine']['principles'])
        self.assertTrue(st['doctrine']['enemy_notes'])

    def test_wall_hybrid_adds_front_layer_not_rear(self):
        # 用户实测反馈：冰坚果被放到最后一排。已有墙时墙系混血应加在墙的前方。
        self.book.bind_one(321,'高冰果')
        b=self.board([321],[Zombie(0,0,0,x=650)],
                     [Plant(0,0,2,320)])          # 回收高坚果墙在 col2
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==321]
        self.assertTrue(cs)
        self.assertTrue(all(c.col>2 for c in cs), f'应在墙前方(>2)，实际 {[c.col for c in cs]}')

    def test_short_range_shooter_needs_reach(self):
        # 用户实测反馈：激光大喷菇被放到最后一排打不着人。
        self.book.kb_by_id[PEA].raw['combat']['range_cells']=4
        far=self.board([PEA],[Zombie(0,0,0,x=750)])   # col≤5 全都够得着(480+320=800)
        cs=[c for c in generate_candidates(far,self.book)
            if c.kind=='plant' and c.type_id==PEA]
        self.assertTrue(cs)
        self.assertEqual(max(c.col for c in cs),5)    # 尽量靠前
        self.book.kb_by_id[PEA].raw['combat']['range_cells']=2
        near_ok=self.board([PEA],[Zombie(0,0,0,x=300)],[Plant(0,0,0,SUN)])
        cs2=[c for c in generate_candidates(near_ok,self.book)
             if c.kind=='plant' and c.type_id==PEA]
        self.assertTrue(cs2 and all(c.col>=2 for c in cs2))  # 2格射程必须贴着僵尸
        out=self.board([PEA],[Zombie(0,0,0,x=650)])
        self.assertFalse([c for c in generate_candidates(out,self.book)
                          if c.kind=='plant' and c.type_id==PEA])  # 全列都够不着→不出候选

    def test_calm_board_still_prebuilds_defence(self):
        # 用户实测反馈：没僵尸就什么都不种不行，要预置防线保持整齐。
        b=self.board([PEA],[],[],sun=800)          # 场上无僵尸、阳光充足
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==PEA]
        self.assertTrue(cs)                        # 仍有预置候选
        self.assertTrue(all(c.score<=26 for c in cs))  # 但分值低，不抢救场资源

    def test_pea_prefers_behind_the_torch_queen(self):
        # 用户技巧：豌豆穿过向日葵女王获火焰增益 → 落点选女王身后。
        b=self.board([PEA],[Zombie(0,0,0,x=650)],[Plant(0,0,2,QUEEN)],sun=900)
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==PEA]
        self.assertTrue(cs)
        self.assertTrue(all(c.col<2 for c in cs), f'应在女王(col2)身后，实际 {[c.col for c in cs]}')

    def test_wall_overlaps_zombie_about_to_eat(self):
        # 用户技巧：僵尸距前排植物 <=2 格时，墙允许与僵尸重叠落子，立刻拦住。
        self.book.bind_one(321,'高冰果')
        b=self.board([321],[Zombie(0,0,0,x=290)],[Plant(0,0,2,SUN)])  # 刚啃穿 col2
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==321]
        self.assertTrue(cs)
        self.assertIn(3,[c.col for c in cs])       # 直接种到僵尸所在格

    def test_cheap_card_stalls_when_no_wall_ready(self):
        # 用户策略（垫背）：危急路没有墙卡可用时，便宜植物垫在僵尸脚下拖时间。
        b=self.board([SUN],[Zombie(0,0,0,x=110)],sun=300)
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==SUN and c.row==0]
        self.assertTrue(cs)
        stall=cs[0]
        self.assertEqual(stall.col,0)              # 僵尸所在格
        self.assertTrue(stall.emergency)
        self.assertIn('speed bump',stall.why)

    def test_economy_first_walls_cannot_outrank_sunflowers_early(self):
        # 用户实测反馈：开局只会种冰坚果墙、不种向日葵。经济未成型（<4 产阳光）时：
        # a) 远僵尸（low）路上墙压不过向日葵；b) 已有一面墙后不再追加；c) 危急路不受限。
        far=self.board([WALL,SUN],[Zombie(0,0,0,x=720)],sun=650)
        cs=generate_candidates(far,self.book)
        wall=[c for c in cs if c.kind=='plant' and c.type_id==WALL]
        suns=[c for c in cs if c.kind=='plant' and c.type_id==SUN]
        self.assertTrue(wall and suns)
        self.assertLess(max(c.score for c in wall), max(c.score for c in suns))
        # 换成僵尸走近（high）时：墙恢复全分，压过向日葵——该拦就拦
        near=self.board([WALL,SUN],[Zombie(0,0,0,x=300)],sun=650)
        cs3=generate_candidates(near,self.book)
        wall3=[c for c in cs3 if c.kind=='plant' and c.type_id==WALL]
        suns3=[c for c in cs3 if c.kind=='plant' and c.type_id==SUN]
        self.assertTrue(wall3 and suns3)
        self.assertGreater(max(c.score for c in wall3), max(c.score for c in suns3))
        # 危急路不受限：僵尸贴脸时墙照样是救场首选（即使已有墙）
        crit=self.board([WALL,SUN],[Zombie(0,0,0,x=110)],[Plant(1,1,4,WALL)],sun=650)
        cs2=generate_candidates(crit,self.book)
        wall2=[c for c in cs2 if c.kind=='plant' and c.type_id==WALL]
        suns2=[c for c in cs2 if c.kind=='plant' and c.type_id==SUN]
        self.assertTrue(wall2)
        self.assertGreater(max(c.score for c in wall2), max(c.score for c in suns2))

    def test_opening_saves_for_queen_before_sunflowers(self):
        # 用户打法：400 开局先等天上掉阳光攒女王，再持续铺向日葵。
        b=self.board([SUN,QUEEN],[],[],sun=400)      # 无僵尸、clock=1000（宽限期内）
        b.game_clock=500
        cs=generate_candidates(b,self.book)
        self.assertFalse([c for c in cs if c.kind=='plant' and c.type_id==SUN],
                         '储蓄期不允许先铺向日葵')
        plan=build_state(b,self.book)['saving_plan']
        self.assertTrue(plan and plan['plant']=='Sunflower Queen')

    def test_opening_queen_planted_when_affordable(self):
        b=self.board([SUN,QUEEN],[],[],sun=650)
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==QUEEN]
        self.assertTrue(cs and cs[0].col in (1,2))
        d=merge_decision(None,generate_candidates(b,self.book),b,self.book)
        self.assertEqual(d.candidate.type_id,QUEEN)   # 储蓄到账必须兑现

    def test_queen_on_field_releases_sunflowers(self):
        # 女王下场后（火炬在场），向日葵恢复正常铺设。
        b=self.board([SUN],[Zombie(0,4,0,x=720)],[Plant(0,0,2,QUEEN)])
        cs=[c for c in generate_candidates(b,self.book)
            if c.kind=='plant' and c.type_id==SUN]
        self.assertTrue(cs)

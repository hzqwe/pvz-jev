"""PvZ 内存偏移表。

来源：pvztoolkit (GPL-3.0, lmintlcx) 的 `src/data.cpp` 中 `data_1_0_0_1051_en`
初始化块，以及 PVZ_Helper (zhumxiang) 的 `PVZ_1_0.cpp` / `Process.cpp`。
两者对 1.0.0.1051 的偏移互相印证。

指针链语义（来自 PVZ_Helper Process.cpp 的 ReadMemory 实现）：
    ReadMemory(size, base, n, off1..offn)
      v = *(u32*)base
      for off in off1..off(n-1): v = *(u32*)(v + off)
      return *(v + offn)          # 取 size 字节

所以：
    LawnApp = *(u32*)0x6A9EC0
    Board   = *(u32*)(LawnApp + 0x768)
    sun     = *(i32*)(Board + 0x5560)

⚠️ 杂交版是重度 mod。下面这张表是**原版 1.0.0.1051 的权威值**，杂交版
大概率沿用（资源包 main.pak 只覆盖资源，exe 仍是 1.0.0.1051），
但必须用 `tools/probe.py` 实测确认。probe 会对可疑字段尝试多种解释。
"""

from __future__ import annotations

VERSION = "1.0.0.1051"

# 原版 PvZ 1.0.0.1051 的映像基址。下面所有 VA 常量都以此为准；
# 若实际基址不同，代码会换算成 base + (VA - PVZ_IMAGE_BASE)。
# 实测杂交版 PlantsVsZombies.exe 就是加载在 0x400000。
PVZ_IMAGE_BASE = 0x400000

# --- 全局 ---------------------------------------------------------------
LAWN_PTR = 0x6A9EC0          # LawnApp* 的静态地址（VA）
PATH_PTR = 0x6A6CC8

# --- LawnApp -> Board ---------------------------------------------------
OFF_BOARD = 0x768
OFF_FRAME_DURATION = 0x454
OFF_GAME_MODE = 0x7F8
OFF_GAME_UI = 0x7FC
OFF_MUSIC = 0x83C

# --- Board 标量 ---------------------------------------------------------
OFF_SUN = 0x5560
OFF_SCENE = 0x554C
OFF_ADVENTURE_LEVEL = 0x5550
OFF_GAME_CLOCK = 0x5568
OFF_ROW_TYPE = 0x5D8
OFF_BLOCK_TYPE = 0x168          # + row*4 + col*0x18
OFF_SPAWN_LIST = 0x6B4
OFF_SPAWN_TYPE = 0x54D4
OFF_DEBUG_MODE = 0x55F8
OFF_CHALLENGE = 0x160
OFF_GAME_PAUSED = 0x164         # bool；菜单打开时为 1（用来判断是否卡在暂停）
OFF_ENDLESS_ROUNDS = 0x16C      # challenge + 0x6C

# --- 植物数组 -----------------------------------------------------------
OFF_PLANT = 0xAC                # -> Plant[] 基址
OFF_PLANT_COUNT_MAX = 0xB0      # 容量
OFF_PLANT_NEXT_POS = 0xB8       # 已用到的下标
# ⚠️★ **步长是 0x304，不是原版的 0x14C**（2026-09-26 实测确认）。
#    杂交版把**植物和僵尸的结构体都加长到了 0x304**。
#    之前写"植物结构体没加长、0x14C 仍正确"是**错的**，而且错法很隐蔽：
#    **第 0 槽在任意步长下地址都一样** —— 只 dump 槽 0 验证字段偏移，永远发现不了
#    步长错误。表现是"草坪上明明有 8 株植物、读取器只报 1 株"（只读到槽 0），
#    于是 agent 以为落点失败、每轮反复往同一格扔卡，而 `generate_candidates`
#    看到的永远是空草坪。**和僵尸步长那次是同一个坑，踩了第二遍。**
#    实测证据：按 0x304 扫出 8 个槽，`(type,row,col)` 与截图逐一对上
#    （9@R1A、0@R2D、161@R1B、78@R2C、101@R3D/R4D、9@R4A、2@R4H）。
#    教训：**验证步长必须至少看 3 个槽**，只看槽 0 等于没验。
PLANT_STRUCT = 0x304
P_ROW = 0x1C
P_TYPE = 0x24
P_COL = 0x28
P_IMITATER = 0x138
P_DEAD = 0x141
P_SQUISHED = 0x142
P_ASLEEP = 0x143
# ★ 植物血量（2026-09-26 按反编译 Lawn/Plant.h 补上）：mPlantHealth=+0x40、
#   mPlantMaxHealth=+0x44、mRecentlyEatenCountdown=+0xB4（>0 = 正在被啃）。
#   为什么可信：杂交版沿用了原版字段布局（P_TYPE=0x24 / P_COL=0x28 /
#   P_ASLEEP=0x143 都和反编译一字不差，只是把结构体步长拉长到 0x304），
#   血量没有理由单独挪走。读取时仍按 `_health()` 的 0<=hp<=max 边界校验，
#   读不出合法值就返回 None（未知），绝不拿垃圾值当血量。
#   （旧表里曾写 P_HP=0xC8 —— 那是照抄僵尸的 mBodyHealth，植物上没有依据，已删除。）
P_HP = 0x40
P_MAX_HP = 0x44
P_RECENTLY_EATEN = 0xB4

# --- 僵尸数组 -----------------------------------------------------------
OFF_ZOMBIE = 0x90               # -> Zombie[] 基址
OFF_ZOMBIE_COUNT_MAX = 0x94     # 容量（实测=8，与 0x304 步长下能读到的对象数一致）
OFF_ZOMBIE_NEXT_POS = 0x9C      # 已分配槽数
OFF_ZOMBIE_COUNT = 0xA0         # 语义未定（实测 3），不要单独依赖
# ⚠️ **步长是 0x304，不是原版的 0x15C**。此前只 dump 了"第 0 槽"验证字段偏移
#    （row@0x1C / x@0x2C / dead@0xEC 全中），但**第 0 槽在哪个步长下地址都一样**，
#    所以那个验证根本没碰到步长问题。实测证据：按 0x304 扫，8 个槽位全部满足
#    "偏移 0x00=LawnApp 且 0x04=Board"（对象反指针），读数像僵尸（row=1 x=579.6
#    dead=0）；按 0x15C 扫，除第 0 槽外全是垃圾（type=-1、x=nan、dead=83）。
#    杂交版确实加长了僵尸结构体 —— 但植物结构体没加长（0x14C 仍正确）。
ZOMBIE_STRUCT = 0x304
Z_ROW = 0x1C
Z_TYPE = 0x24
Z_PHASE = 0x28
# ⚠️ Z_X / Z_Y 是**游戏内部的 800x500 逻辑坐标**，不是屏幕像素。
#    实测 y = 50 + 100*row（row=1->150、row=4->450），x 在 0..800 之间。
#    换算到屏幕：sx = grid_left + x*(草坪宽/800)，sy = grid_top + y*(草坪高/500)。
Z_X = 0x2C
Z_Y = 0x30
Z_DEAD = 0xEC
# Original field layout: ruslan831/PlantsVsZombies-decompilation, Lawn/Zombie.h.
# Hybrid 3.9.9 body/helmet pairs checked read-only on 2026-09-26; reject invalid pairs.
Z_HP, Z_MAX_HP = 0xC8, 0xCC
Z_HELM_HP, Z_HELM_MAX_HP = 0xD0, 0xD4
Z_SHIELD_HP, Z_SHIELD_MAX_HP = 0xDC, 0xE0
Z_FRIENDLY = 0xB8

# --- 对象槽位"是否真的有对象"的判据 -------------------------------------
# ⚠️ 本项目最深的坑：PvZ 的对象数组是**预分配池**，未使用槽位**全零**。
#    而零值恰好是合法数据 —— type=0 是豌豆射手、row=col=0 是 A1、dead=0 是"活着"。
#    于是每个空槽都伪装成一个真实单位（实测凭空多出 5 只"R1 的僵尸"、3 株
#    "R1A 的豌豆射手"，且僵尸 x=0）。光靠 dead 标志区分不了，因为空槽 dead 也是 0。
#    可靠判据：真实对象在槽首存了 **LawnApp / Board 反指针**（实测植物
#    0x00=0x040AA000=LawnApp、0x04=0x63DA89E0=Board；僵尸同构），空槽是 0。
LIVE_LAWN_PTR = 0x00
LIVE_BOARD_PTR = 0x04

# --- 小推车 -------------------------------------------------------------
OFF_MOWER = 0x100
OFF_MOWER_DEAD = 0x30
OFF_MOWER_COUNT_MAX = 0x104
OFF_MOWER_COUNT = 0x110
MOWER_STRUCT = 0x48
M_ROW = 0x14
M_STATE = 0x2C
M_READY = 1

# --- 场地物品（墓碑/梯子/弹坑…）----------------------------------------
OFF_GRID_ITEM = 0x11C
OFF_GRID_ITEM_COUNT_MAX = 0x120
GRID_ITEM_STRUCT = 0xEC
GI_TYPE = 0x08
GI_COL = 0x10
GI_ROW = 0x14
GI_DEAD = 0x20

# --- 光标对象（CursorObject）-------------------------------------------
# ★ 本项目最有用的一组偏移（2026-09-26 实测确认）。
#   Board+0x138 -> CursorObject*，CursorObject+0x30 = "手上有没有拿东西"：
#       0 = 空手，1 = 手持种子。
#   +0x24 = 被拿起的**卡槽下标**，+0x28 = 该卡的 **type_id**。
#
#   为什么它比"阳光差值"可靠得多：杂交版**点卡不扣阳光、放置才扣**
#   （实测：点卡后 sun 740->740，落点后 865->740）。旧代码拿"点卡后阳光没掉"
#   当"卡被拒"，于是**每一次点卡都被误判成"太贵"**，把成本模型污染成
#   "每株都要 900+ 阳光"，最终所有候选都被过滤掉 —— 表现就是「植物种不下去」。
#   cursor_grab 是游戏自己维护的状态，不受阳光涨落影响。
#   附带好处：+0x24/+0x28 是**卡槽绑定的 ground truth**，比冷却指纹可靠。
OFF_CURSOR = 0x138
C_GRAB = 0x30                   # 实为 mCursorType 枚举（见下），0=空手
C_SLOT = 0x24                   # 手持的卡槽下标（mSeedBankIndex）
C_TYPE = 0x28                   # 手持的 type_id（mType）
# ★ mCursorType 的完整枚举（2026-09-26 按反编译 ConstEnums.h 补全）：
#   0=NORMAL(空手) 1=PLANT_FROM_BANK(手持种子) ... 6=SHOVEL(铲子)。
#   此前只把它当 0/1 的"抓取标志"用；现在铲子支持需要区分 1 和 6。
CUR_NORMAL = 0
CUR_PLANT_FROM_BANK = 1
CUR_SHOVEL = 6

# --- 种子栏（SeedBank）-------------------------------------------------
# pvztoolkit: slot 0x144 是 **指针**，slot_count 0x24 是相对 slot 基址的子偏移；
# 每个卡槽结构体步长 0x50，卡内字段：冷却已过 0x4C、冷却总时长 0x50、卡片类型 0x5C。
# 这里给出 probe 需要尝试的几种解释，最终以实测为准。
OFF_SLOT = 0x144
SLOT_STRUCT = 0x50
S_COUNT = 0x24
S_CD_PAST = 0x4C
S_CD_TOTAL = 0x50
S_SEED_TYPE = 0x5C
S_SEED_TYPE_IM = 0x60

# --- 场地尺寸 -----------------------------------------------------------
LAWN_ROWS = 5
LAWN_COLS = 9
# ⚠️ 实测杂交版这一局 scene=19，超出 0..5 的范围（原版 SetScene 只接受 0..5）。
# 原版 scene 0..5 -> 背景 id {1,2,3,4,5,7}（pvztoolkit SetScene 的映射表）。
# 19 很可能是杂交版自己的背景枚举，**不能**按原版下标查行数，
# 因此行数一律以「僵尸/植物的 row 字段实测最大值」为准，不用本表。
SCENE_ROWS = {0: 5, 1: 5, 2: 6, 3: 6, 4: 5}

# --- 界面状态（LawnApp+0x7FC）------------------------------------------
# ⚠️ 已核对 pvztoolkit 源码：GetScene() 只在 `ui == 2 || ui == 3` 时读 scene，
#    且多处功能用 `if (GameUI() != 3) return;` 作前置条件 —— 说明 **3 = 对局中**。
#    （此前 README 里"2=对局"的说法是错的，实测本机 ui=3 时确实在读对局字段。）
#    已证实：1 = 主菜单（pvz.cpp:614 用 `GameUI()==1` 判"还没开始冒险"）、3 = 对局中。
#    2 只在 GetScene 的条件里出现，语义未证实，不要依赖。
UI_MENU = 1
UI_PLAYING = 3

# Hybrid 3.9.9 coin pool: 14 consecutive live back-pointer pairs at 0x104 spacing
# verified read-only 2026-09-26. Original Coin fields remain at these offsets.
OFF_COINS, OFF_COINS_CAP = 0xE4, 0xE8
COIN_STRUCT = 0x104
COIN_USABLE_SEED = 16
CUR_PLANT_FROM_DROP = 2

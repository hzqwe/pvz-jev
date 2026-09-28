"""Offline snapshot replay, no model calls or game input.

Logs lack exact entity IDs and intermediate execution snapshots. This verifies
candidate legality/semantics and learning exclusions, not match outcomes or wins.
"""
import argparse
import json
import re
import sys
from dataclasses import fields
from unittest.mock import patch
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pvz.agent import PvZJevAgent
from pvz.board import BoardState,Plant,Zombie,SeedSlot,DroppedSeed
from pvz.plants import PlantBook
from pvz.policy import generate_candidates,action_invalid_reason


def snapshot(record,book,legacy_names=None):
    raw=record.get('board')
    if raw:
        excluded={'plants','zombies','slots','dropped_seeds','row_types','mowers','snow_cells','rejected_cells'}
        b=BoardState(**{f.name:raw[f.name] for f in fields(BoardState)
                        if f.name in raw and f.name not in excluded})
        for key,cls in [('plants',Plant),('zombies',Zombie),('slots',SeedSlot),('dropped_seeds',DroppedSeed)]:
            setattr(b,key,[cls(**d) for d in raw.get(key,[])])
        b.row_types={int(r):v for r,v in raw.get('row_types',{}).items()}
        b.mowers={int(r):v for r,v in raw.get('mowers',{}).items()}
        b.snow_cells={(r,c):t for r,c,t in raw.get('snow_cells',[])}
        b.rejected_cells={(r,c):t for r,c,t in raw.get('rejected_cells',[])}
        cursor=raw.get('cursor',{})
        b.holding=cursor.get('holding',False);b.held_cursor=cursor.get('kind',0)
        b.held_type=cursor.get('type_id',-1);b.held_slot=cursor.get('slot',-1)
        return b
    state=record['state'];game=state['game']
    # The explicit version may rename a verified profile. Keep its pre-overlay
    # English alias for older state-only logs; never infer IDs from card order.
    names=dict(legacy_names or {})
    names.update({book.en(t):t for t in book.kb_by_id})
    names.update(book.bound_ids)
    if book.catalog and (book.catalog.edition, book.catalog.game_version) == ('classic', '3.9.9'):
        # State-only logs before the version catalogue called verified type16
        # "Lily Pad". Keep this explicit historical alias; it is not a generic
        # inference that every lily/pad card is interchangeable.
        names.setdefault('Lily Pad', 16)
        names.setdefault('Peashooter Lily Pad', 16)
    water={lane['lane']-1 for lane in state['lanes'] if lane.get('terrain','').startswith('water')}
    board=BoardState(ok=True,sun=game['sun'],rows=game['rows'],cols=game['cols'],
        scene=2 if water else 0,level=game.get('level'),game_clock=game['clock'],clock_advancing=True)
    board.row_types={r:2 if r in water else 1 for r in range(board.rows)}
    for lane in state['lanes']:
        row=lane['lane']-1
        mower=lane.get('tactical_assessment',{}).get('mower_available')
        if mower is not None:board.mowers[row]=mower
        for p in lane['defenders']:
            tid=names[p['plant']];col=ord(p['column'])-ord('A')
            board.plants.append(Plant(row*1000+col*200+tid,row,col,tid,
                asleep=p.get('asleep',False),hp=p.get('hp'),recently_eaten=p.get('recently_eaten',False)))
        for z in lane['zombies']:
            tid=int(re.search(r'zombie_type_(\d+)',z['kind'])[1])
            board.zombies.append(Zombie(len(board.zombies),row,tid,x=z['x_px'],
                hp=z.get('body_hp'),armor_hp=z.get('armor_hp'),
                stationary=z.get('traits',{}).get('stationary_maybe_ranged')))
    for s in state['seed_cards']:
        board.slots.append(SeedSlot(s['slot'],names[s['plant']],0 if s['ready'] else 500,1000))
    return board


def replay(path,game_version=None):
    book=PlantBook(hybrid_file='',cost_file='')
    legacy_names={book.en(t):t for t in book.kb_by_id}
    if game_version is not None:
        book.activate_catalog('classic',game_version)
    records=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    invalid=[];count=Counter(missing_reasons=0,overlong_descriptions=0);previous=None;previous_record=None
    for i,record in enumerate(records):
        board=snapshot(record,book,legacy_names)
        if book.catalog is not None:
            for slot in board.slots:
                book.bind_identity(slot.type_id)
            for plant in board.plants:
                book.bind_identity(plant.type_id)
        if previous is not None and board.game_clock<previous.game_clock:
            book.runtime_crush.clear();book.crush_evidence.clear();previous=None
        if previous is not None:
            for step in previous_record.get('executed',{}).get('steps',[]):
                if step['step'] in ('source_removed','salvaged'):
                    for p in previous.plants:
                        if list(p.cell)==step['cell']:
                            board.intentional_removals.add((p.row,p.col,p.type_id))
            notes=PvZJevAgent.learn_crush_events(previous,board,book)
            count['soft_observations']+=len(notes)
        instant=record.get('timing',{}).get('snapshot_at') or float(record['t'])
        with patch('pvz.board.time.time',return_value=instant):
            for c in generate_candidates(board,book):
                count['candidates']+=1
                if 'Reason:' not in c.describe(book):count['missing_reasons']+=1
                if len(c.describe(book))>360:count['overlong_descriptions']+=1
                if c.kind=='plant':
                    count['plant_candidates']+=1
                    reason=action_invalid_reason(c,board,book)
                    if reason:invalid.append({'line':i+1,'type':c.type_id,'cell':[c.row,c.col],'reason':reason})
        previous,previous_record=board,record
    return dict(game_version=book.catalog.game_version if book.catalog else None,
                snapshots=len(records),**count,invalid_plant_candidates=invalid,
                hard_runtime_crush_types=sorted(book.runtime_crush),
                limitation='Current policy and plant book replay; legacy positions are rounded. No live Jev, input, or match outcome simulation.')


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--log',type=Path,default=ROOT/'out/decisions_20260927_235811.jsonl')
    ap.add_argument('--game-version',help='Explicit classic version for versioned mechanics; omitted uses legacy bindings.')
    args=ap.parse_args()
    report=replay(args.log,game_version=args.game_version)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    sys.exit(bool(report['invalid_plant_candidates'] or report.get('missing_reasons') or report.get('overlong_descriptions')))

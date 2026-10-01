"""Replay observed waits with historical Jev answers; no model calls or game input.

Each snapshot is independent. Action IDs are remapped by their recorded meaning;
unavailable actions are reported as unresolved instead of pretending the model
chose another action. This measures policy changes, not outcomes or win rates.
"""
import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pvz.jev import JevAnswer, JevResponse
from pvz.plants import PlantBook
from pvz.policy import generate_candidates, merge_decision, action_invalid_reason
from tools.replay_battle_review import snapshot


def action_meaning(c):
    keys = ('kind', 'row', 'col', 'slot', 'type_id', 'supports_type',
            'replacement_type', 'source_index', 'drop_index', 'relocate_to',
            'salvage', 'intercept', 'crush_recovery')
    raw = c if isinstance(c, dict) else vars(c)
    return tuple(tuple(raw[k]) if isinstance(raw.get(k), list) else raw.get(k)
                 for k in keys)


def replay(path):
    book = PlantBook(hybrid_file='', cost_file='', ids_file='')
    book.activate_catalog('classic', '3.9.9')
    result = {'log': path.name, 'observed_waits': 0, 'rich_observed_waits': 0,
              'changed': [], 'still_waiting': [], 'unresolved': [], 'invalid': []}
    for line, text in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not text.strip():
            continue
        record = json.loads(text)
        if record.get('rapid_fill') or not record.get('decision', {}).get('hold'):
            continue
        result['observed_waits'] += 1
        board = snapshot(record, book)
        rich = (board.sun or 0) >= 1000
        result['rich_observed_waits'] += int(rich)
        for tid in {s.type_id for s in board.slots} | {p.type_id for p in board.plants}:
            book.bind_identity(tid)
        answers = {qid: JevAnswer(qid, data['kind'], dict(data['raw']))
                   for qid, data in (record.get('jev') or {}).get('answers', {}).items()}
        with patch('pvz.board.time.time', return_value=record['t']):
            candidates = generate_candidates(board, book)
            action = answers.get('action')
            if action:
                old = next((c for c in record.get('candidates', [])
                            if c['cid'] == action.choice), None)
                mapped = next((c for c in candidates if old and
                               action_meaning(c) == action_meaning(old)), None)
                if mapped is None:
                    result['unresolved'].append({'line': line, 'sun': board.sun,
                        'reason': 'Historical model action is absent from current shortlist.'})
                    continue
                action.raw['choice'] = mapped.cid
            response = JevResponse(answers=answers,
                                   error=(record.get('jev') or {}).get('error'))
            d = merge_decision(response, candidates, board, book)
            item = {'line': line, 'sun': board.sun, 'action': d.candidate.kind,
                    'row': d.candidate.row, 'col': d.candidate.col,
                    'type_id': d.candidate.type_id,
                    'followup_type': d.candidate.supports_type,
                    'replacement_type': d.candidate.replacement_type,
                    'cost': d.candidate.total_cost(book), 'notes': d.notes}
            result['still_waiting' if d.hold else 'changed'].append(item)
            reason = action_invalid_reason(d.candidate, board, book)
            if reason:
                result['invalid'].append({'line': line, 'reason': reason})
    result['rich_changed_count'] = sum(item['sun'] >= 1000 for item in result['changed'])
    result['rich_still_waiting_count'] = sum(item['sun'] >= 1000 for item in result['still_waiting'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    reports = [replay(path) for path in args.log]
    payload = json.dumps({'reports': reports, 'limitation': __doc__}, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + '\n', encoding='utf-8')
    for report in reports:
        print(json.dumps({key: len(value) if isinstance(value, list) else value
                          for key, value in report.items()}, ensure_ascii=False))
    sys.exit(any(report['invalid'] for report in reports))

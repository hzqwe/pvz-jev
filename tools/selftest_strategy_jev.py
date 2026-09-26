"""Live Jev API smoke test on synthetic boards; never touches the game.

Uses the configured TypeSafe API key and makes three billable requests.
"""
import json
from pathlib import Path

from test_strategy import StrategyTests, SUN, PEA, STRONG, WALL, BOMB, FREEZE
from pvz.board import Plant, Zombie
from pvz.policy import generate_candidates, build_questions, merge_decision
from pvz.serialize import build_state
from pvz.jev import JevClient


def main():
    fixture = StrategyTests()
    fixture.setUp()
    book = fixture.book
    plants = [Plant(r, r, 0, SUN) for r in range(4)] + [Plant(10, 0, 2, PEA)]
    saving = fixture.board([SUN, PEA, STRONG], [Zombie(0, 0, 0, x=720)], plants, sun=400)
    rescue = fixture.board([BOMB, WALL, FREEZE, SUN], [Zombie(0, 4, 0, x=110)], sun=300)
    rescue.mowers = {4: False}
    upgrade = fixture.board([SUN, PEA, STRONG], [Zombie(0, 0, 0, x=720)], plants, sun=500)
    client = JevClient(timeout=12, retries=2)
    results = []
    output = Path(__file__).resolve().parents[1] / 'out' / 'strategy-validation.json'
    output.parent.mkdir(exist_ok=True)
    for name, board in [('saving', saving), ('house_rescue', rescue), ('buy_upgrade', upgrade)]:
        candidates = generate_candidates(board, book)
        response = client.ask(build_state(board, book), build_questions(candidates, board, book))
        if not response.ok:
            raise RuntimeError(f'{name}: {response.error}')
        decision = merge_decision(response, candidates, board, book)
        candidate = decision.candidate
        passed = (decision.hold if name == 'saving' else
                  not decision.hold and candidate.emergency if name == 'house_rescue' else
                  not decision.hold and candidate.type_id == STRONG)
        result = dict(scenario=name, passed=passed, model=response.model,
                      latency_s=round(response.latency_s, 2),
                      answers={key: answer.raw for key, answer in response.answers.items()},
                      action=candidate.describe(book), hold=decision.hold,
                      fallback=decision.fallback, notes=decision.notes)
        results.append(result)
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'{name}: passed={passed}, latency={response.latency_s:.2f}s, '
              f'fallback={decision.fallback}, action={book.en(candidate.type_id) if not decision.hold else "WAIT"}', flush=True)
        if not passed:
            raise AssertionError(f'{name}: inspect {output}')
    print(f'Results: {output}')


if __name__ == '__main__':
    main()

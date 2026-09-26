"""Live Jev API smoke test on synthetic boards; never touches the game.

Uses the configured TypeSafe API key and makes four billable requests.
"""
import json
from pathlib import Path

from test_strategy import StrategyTests, SUN, PEA, STRONG, WALL, BOMB, FREEZE
from pvz.board import Plant, Zombie
from pvz.policy import generate_candidates, build_questions, merge_decision
from pvz.serialize import build_state
from pvz.jev import JevClient

RECLAIM = 320   # 回收高坚果（hybrid_plants.json 里有完整登记）


CONFIDENCE_FLOOR = 0.3   # 低于此值=argmax 险过，结果算 passed 但要人工复核


def main():
    fixture = StrategyTests()
    fixture.setUp()
    book = fixture.book
    assert book.bind_one(RECLAIM, '回收高坚果')
    plants = [Plant(r, r, 0, SUN) for r in range(4)] + [Plant(10, 0, 2, PEA)]
    saving = fixture.board([SUN, PEA, STRONG], [Zombie(0, 0, 0, x=720)], plants, sun=400)
    rescue = fixture.board([BOMB, WALL, FREEZE, SUN], [Zombie(0, 4, 0, x=110)], sun=300)
    rescue.mowers = {4: False}
    upgrade = fixture.board([SUN, PEA, STRONG], [Zombie(0, 0, 0, x=720)], plants, sun=500)
    # 回收场景：一面马上要跌破回收线的回收高坚果（1000 血，再啃两秒就低于
    # 800 —— 那之后就永远收不回来了）+ 正在啃它的僵尸。卡槽为空 -> 没有任何
    # 种植候选，唯一留住这面墙的办法就是现在铲掉它收回卡片。
    reclaim = fixture.board([], [Zombie(0, 0, 0, x=250)],
                            [Plant(0, 0, 2, RECLAIM, hp=1000, recently_eaten=True)], sun=1000)
    client = JevClient(timeout=12, retries=2)
    results = []
    output = Path(__file__).resolve().parents[1] / 'out' / 'strategy-validation.json'
    output.parent.mkdir(exist_ok=True)
    needs_review = []
    for name, board in [('saving', saving), ('house_rescue', rescue),
                        ('buy_upgrade', upgrade), ('reclaim_shovel', reclaim)]:
        candidates = generate_candidates(board, book)
        response = client.ask(build_state(board, book), build_questions(candidates, board, book))
        if not response.ok:
            raise RuntimeError(f'{name}: {response.error}')
        decision = merge_decision(response, candidates, board, book)
        candidate = decision.candidate
        passed = (decision.hold if name == 'saving' else
                  not decision.hold and candidate.emergency if name == 'house_rescue' else
                  not decision.hold and candidate.kind == 'shovel' if name == 'reclaim_shovel' else
                  not decision.hold and candidate.type_id == STRONG)
        action_answer = response.get('action')
        conf = action_answer.confidence if action_answer else None
        # 2026-09-26 加严：实测 buy_upgrade 0.11 / reclaim_shovel 0.06 都是
        # argmax 险过 —— 这样的"通过"拦不住实战问题，必须显式标记出来。
        low_conf = conf is not None and conf < CONFIDENCE_FLOOR
        if low_conf:
            needs_review.append(name)
        result = dict(scenario=name, passed=passed, confidence=conf,
                      needs_review=low_conf, model=response.model,
                      latency_s=round(response.latency_s, 2),
                      answers={key: answer.raw for key, answer in response.answers.items()},
                      action=candidate.describe(book), hold=decision.hold,
                      fallback=decision.fallback, notes=decision.notes)
        results.append(result)
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        label = ('WAIT' if decision.hold else
                 f'{candidate.kind}:{book.en(candidate.type_id)}')
        print(f'{name}: passed={passed}, conf={conf}, '
              f'latency={response.latency_s:.2f}s, '
              f'fallback={decision.fallback}, action={label}', flush=True)
        if not passed:
            raise AssertionError(f'{name}: inspect {output}')
    if needs_review:
        print(f'⚠️ 这些场景置信度 <{CONFIDENCE_FLOOR}（argmax 险过，需人工复核）：'
              f'{", ".join(needs_review)}')
    print(f'Results: {output}')


if __name__ == '__main__':
    main()

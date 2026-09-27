"""Battle review detectors must fire on known bad patterns — synthetic records only."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.review_battle import detect, kpis, load_records, render


def rec(t, sun=500, clock=300, producers=2, threat="none", executed=None,
        fallback=None, jev_ok=True, latency=0.5):
    """构造一条与真实日志同构的决策记录。"""
    lanes = []
    if threat != "none":
        lanes.append({"lane": 1, "threat_level": threat,
                      "defenders": [{"role": "producer"}] * producers})
    for i in range(2, 6):
        lanes.append({"lane": i, "threat_level": "none",
                      "defenders": [{"role": "producer"}] * producers})
    return {
        "t": str(t), "iso": f"2026-09-26 12:00:{int(t) % 60:02d}",
        "state": {"game": {"sun": sun, "clock": clock, "rows": 5},
                  "lanes": lanes},
        "candidates": [], "decision": {"fallback": fallback},
        "executed": executed or {"kind": "hold"},
        "jev": {"ok": jev_ok, "latency_s": latency},
    }


class ReviewTests(unittest.TestCase):
    def test_plant_failure_with_wasted_sun_is_critical(self):
        recs = [rec(100, sun=190, clock=3000,
                    executed={"kind": "click", "placed": False, "grid": "r2c2",
                              "note": "落点未生效", "sun_before": 190, "sun_after": 65})
                for _ in range(2)]
        kinds = {f["kind"] for f in detect(recs)}
        self.assertIn("落点失败还扣了阳光", kinds)
        self.assertIn("落点反复失败", kinds)
        top = next(f for f in detect(recs) if f["kind"] == "落点失败还扣了阳光")
        self.assertIn("250 阳光", top["evidence"])

    def test_plant_name_falls_back_to_decision_text(self):
        recs = [rec(100, clock=3000,
                    executed={"kind": "click", "placed": False, "grid": "r0c2",
                              "sun_before": 200, "sun_after": 200},
                    ) for _ in range(2)]
        recs[0]["decision"]["chosen"] = 'Plant "Peashooter" (shooter) at lane 1'
        recs[1]["decision"]["chosen"] = 'Plant "Peashooter" (shooter) at lane 1'
        ev = [f["evidence"] for f in detect(recs) if f["kind"] == "落点反复失败"]
        self.assertTrue(ev and "Peashooter" in ev[0])

    def test_cadence_stall_detected(self):
        recs = [rec(0), rec(1), rec(40), rec(41)]
        kinds = {f["kind"]: f["evidence"] for f in detect(recs)}
        self.assertIn("决策节奏空档", kinds)
        self.assertIn("39", kinds["决策节奏空档"])

    def test_economy_collapse_and_zero_producer_window(self):
        recs = [rec(100 + i * 2, sun=60, clock=20000, producers=0) for i in range(6)]
        kinds = {f["kind"] for f in detect(recs)}
        self.assertIn("经济崩盘（中盘赤字）", kinds)
        self.assertIn("零产出窗口", kinds)

    def test_hold_during_critical_is_flagged(self):
        recs = [rec(10 + i, threat="critical",
                    executed={"kind": "hold"}) for i in range(3)]
        self.assertIn("危急时仍在等待", {f["kind"] for f in detect(recs)})

    def test_high_fallback_rate_is_flagged(self):
        recs = [rec(10 + i, fallback=True) for i in range(10)]
        self.assertIn("模型兜底率偏高", {f["kind"] for f in detect(recs)})

    def test_clean_game_triggers_nothing(self):
        recs = [rec(10 + i * 2, sun=400, clock=300, producers=3,
                    executed={"kind": "click", "placed": True, "grid": "r0c2",
                              "sun_before": 400, "sun_after": 300})
                for i in range(10)]
        self.assertEqual(detect(recs), [])

    def test_kpis_and_render(self):
        recs = [rec(10 + i * 2, sun=300 + i,
                    executed={"kind": "click", "placed": True, "grid": "r0c2",
                              "sun_before": 300, "sun_after": 200}) for i in range(5)]
        k = kpis(recs)
        self.assertEqual(k["placed"], 5)
        self.assertEqual(k["sun_first"], 300)
        text = render(recs, "decisions_x.jsonl", detect(recs))
        self.assertIn("战斗复盘", text)
        self.assertIn("闸门", text)

    def test_malformed_lines_are_skipped(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as fh:
            fh.write("{broken json\n")
            fh.write(json.dumps(rec(1)) + "\n")
            path = fh.name
        try:
            self.assertEqual(len(load_records(path)), 1)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()

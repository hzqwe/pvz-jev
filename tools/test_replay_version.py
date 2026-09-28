"""Replays must select an explicit version rather than silently guess one."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.replay_battle_review import replay


class ReplayVersionTests(unittest.TestCase):
    def test_explicit_version_resolves_catalog_for_raw_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'snapshots.jsonl'
            path.write_text(json.dumps({'t':1,'board':{'ok':True,'sun':125,
                'rows':5,'cols':9,'game_clock':10000,'slots':[
                    {'index':0,'type_id':0,'cd_left':0,'cd_total':750}]}}),encoding='utf-8')
            report = replay(path,game_version='3.9.9')
            self.assertEqual(report['game_version'],'3.9.9')
            self.assertEqual(report['snapshots'],1)
            self.assertEqual(report['invalid_plant_candidates'],[])

    def test_unsupported_version_is_rejected_even_without_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'empty.jsonl'
            path.write_text('',encoding='utf-8')
            with self.assertRaises(ValueError):
                replay(path,game_version='3.19')

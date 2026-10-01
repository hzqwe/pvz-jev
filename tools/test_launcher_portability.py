"""Exercise the batch entry point with an interpreter spy, never the real game launcher."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'The batch entry point is Windows-only')
class LauncherPortabilityTests(unittest.TestCase):
    def run_launcher(self, include_tools):
        with tempfile.TemporaryDirectory(prefix='pvz-launcher-') as folder:
            base = Path(folder)
            project = base / '临时 project & tools'
            project.mkdir()
            launcher = project / 'start.bat'
            launcher.write_bytes((ROOT / '启动Jev.bat').read_bytes())
            if include_tools:
                (project / 'tools').mkdir()
                (project / 'tools/launch.py').write_text('# Entry marker only.\n', encoding='utf-8')
            outside = base / 'outside'
            outside.mkdir()
            mock_bin = base / 'mock-bin'
            mock_bin.mkdir()
            result = base / 'result.json'
            spy = base / 'spy.py'
            spy.write_text(
                'import json, os, sys\n'
                'from pathlib import Path\n'
                'Path(os.environ["PVZ_LAUNCH_TEST_RESULT"]).write_text(json.dumps({\n'
                '  "cwd": os.getcwd(), "argv": sys.argv[1:],\n'
                '  "utf8": os.environ.get("PYTHONUTF8")\n'
                '}), encoding="utf-8")\n', encoding='utf-8')
            interpreter = mock_bin / 'python-spy.cmd'
            interpreter.write_text('@echo off\n"%PVZ_LAUNCH_TEST_PYTHON%" "%PVZ_LAUNCH_TEST_SPY%" %*\n',
                                   encoding='ascii')
            (mock_bin / 'py.cmd').write_text('@echo off\necho %PVZ_LAUNCH_TEST_INTERPRETER%\n', encoding='ascii')
            env = dict(os.environ, PATH=str(mock_bin) + os.pathsep + os.environ.get('PATH', ''),
                       PVZ_LAUNCH_TEST_RESULT=str(result), PVZ_LAUNCH_TEST_PYTHON=sys.executable,
                       PVZ_LAUNCH_TEST_SPY=str(spy), PVZ_LAUNCH_TEST_INTERPRETER=str(interpreter))
            command = f'"{env["COMSPEC"]}" /d /s /c ""{launcher}" marker "two words""'
            completed = subprocess.run(command, cwd=outside, env=env, input=b'\n',
                                       capture_output=True, timeout=15)
            data = json.loads(result.read_text(encoding='utf-8')) if result.exists() else None
            return completed.returncode, data, project

    def test_relocated_checkout_launches_its_own_entry_and_preserves_arguments(self):
        code, data, project = self.run_launcher(include_tools=True)
        self.assertEqual(code, 0)
        self.assertIsNotNone(data)
        self.assertEqual(Path(data['cwd']), project)
        self.assertEqual(Path(data['argv'][0]), project / 'tools/launch.py')
        self.assertEqual(data['argv'][1:], ['marker', 'two words'])
        self.assertEqual(data['utf8'], '1')

    def test_incomplete_checkout_exits_without_calling_another_repository(self):
        code, data, _ = self.run_launcher(include_tools=False)
        self.assertEqual(code, 1)
        self.assertIsNone(data)


if __name__ == '__main__':
    unittest.main()

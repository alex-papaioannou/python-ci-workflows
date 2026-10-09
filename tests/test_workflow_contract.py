"""Execute the actual embedded workflow runner against temporary consumers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).parents[1] / '.github/workflows/python-ci.yml'

def script(name):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index('      - name: ' + name)
    start = lines.index('        run: |', start) + 1
    block = []
    for line in lines[start:]:
        if line and not line.startswith('          '):
            break
        block.append(line[10:])
    return '\n'.join(block)

class WorkflowTests(unittest.TestCase):
    def execute(self, files, env=None, step='Run tests and reject empty test suites'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            process_env = os.environ.copy()
            process_env.update(env or {})
            return subprocess.run([sys.executable, '-c', script(step)], cwd=root,
                                  env=process_env, text=True, capture_output=True)

    def test_success_and_consumer_root_import(self):
        result = self.execute({'lib.py': 'VALUE=7', 'tests/test_ok.py':
            'import unittest\nfrom lib import VALUE\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(VALUE,7)'})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failure_propagates(self):
        result = self.execute({'tests/test_bad.py':
            'import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail("expected")'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('expected', result.stderr)

    def test_empty_suite_rejected(self):
        result = self.execute({'tests/empty.py': ''})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No tests discovered', result.stderr)

    def test_import_error_rejected(self):
        result = self.execute({'tests/test_import.py': 'import nonexistent_fixture_module'})
        self.assertNotEqual(result.returncode, 0)

    def test_syntax_error_rejected(self):
        result = self.execute({'bad.py': 'def broken('}, step='Check Python syntax')
        self.assertNotEqual(result.returncode, 0)

    def test_custom_test_directory(self):
        result = self.execute({'checks/test_ok.py': 'import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass'}, {'TEST_DIRECTORY': 'checks'})
        self.assertEqual(result.returncode, 0, result.stderr)

"""Guard against shipping code with a stale runtime version label."""
from __future__ import annotations

from pathlib import Path
import sys
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class ReleaseVersionTests(unittest.TestCase):
    def test_runtime_matches_project_metadata(self):
        pyproject = ROOT / 'pyproject.toml'
        if not pyproject.is_file():
            self.skipTest('Standalone module patch does not contain full project metadata')
        from frontier_register import __version__
        package = tomllib.loads(pyproject.read_text(encoding='utf-8'))
        self.assertEqual(package['project']['version'], __version__)


if __name__ == '__main__':
    unittest.main()

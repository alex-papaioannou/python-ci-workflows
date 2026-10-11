"""Tests for the changelog parser that drives the release workflow."""

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / ".github/scripts/release_notes.py"
spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
assert spec and spec.loader
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)


class NewestReleaseTests(unittest.TestCase):
    def test_takes_first_section_only(self):
        changelog = "# Changelog\n\nIntro.\n\n## [1.2.3]\n\n### Added\n\n- New.\n\n## [1.2.2]\n\n- Old.\n"
        version, major, notes = release_notes.newest_release(changelog)
        self.assertEqual((version, major), ("1.2.3", "v1"))
        self.assertEqual(notes, "### Added\n\n- New.\n")

    def test_last_section_runs_to_end(self):
        _, _, notes = release_notes.newest_release("## [2.0.0]\n\nOnly.\n")
        self.assertEqual(notes, "Only.\n")

    def test_missing_heading_rejected(self):
        with self.assertRaises(SystemExit):
            release_notes.newest_release("## [Unreleased]\n\n- Pending.\n")

    def test_repository_changelog_parses(self):
        changelog = (SCRIPT.parents[2] / "CHANGELOG.md").read_text(encoding="utf-8")
        version, major, notes = release_notes.newest_release(changelog)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertTrue(major.startswith("v"))
        self.assertTrue(notes.strip())

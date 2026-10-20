"""Tests for the changelog parser that drives the release workflow."""

import importlib.util
import os
import subprocess
import sys
import tempfile
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

    def test_dated_heading_is_released(self):
        changelog = "## [1.1.0] - 2026-10-20\n\n- New.\n\n## [1.0.0]\n\n- Old.\n"
        self.assertEqual(
            release_notes.newest_release(changelog), ("1.1.0", "v1", "- New.\n")
        )

    def test_unreleased_is_skipped_for_the_next_version(self):
        changelog = "## [Unreleased]\n\n- Pending.\n\n## [1.1.0]\n\n- Shipped.\n"
        self.assertEqual(
            release_notes.newest_release(changelog), ("1.1.0", "v1", "- Shipped.\n")
        )

    def test_malformed_headings_fail_with_the_heading(self):
        for heading in (
            "[v1.1.0]",
            "[2.0.0-rc.1]",
            "1.1.0",
            "[1.1.0] 2026-10-20",
            "[1.1]",
        ):
            with self.subTest(heading=heading):
                with self.assertRaises(SystemExit) as raised:
                    release_notes.newest_release(
                        f"## {heading}\n\n- New.\n\n## [1.0.0]\n\n- Old.\n"
                    )
                self.assertIn(f"'## {heading}'", str(raised.exception))

    def test_empty_section_fails(self):
        with self.assertRaises(SystemExit) as raised:
            release_notes.newest_release("## [1.1.0]\n\n## [1.0.0]\n\n- Old.\n")
        self.assertIn("has no release notes", str(raised.exception))

    def test_subsection_headings_are_part_of_the_notes(self):
        _, _, notes = release_notes.newest_release(
            "## [1.1.0]\n\n### Fixed\n\n- Bug.\n"
        )
        self.assertEqual(notes, "### Fixed\n\n- Bug.\n")


class StrictHeadingTests(unittest.TestCase):
    def test_versions_must_be_ascii_semver_numbers(self):
        for heading in ("[01.0.0]", "[1.02.0]", "[1.1.00]", "[\uff11.1.0]"):
            with self.subTest(heading=heading), self.assertRaises(SystemExit):
                release_notes.newest_release(
                    f"## {heading}\n\n- New.\n\n## [1.0.0]\n\n- Old.\n"
                )

    def test_version_headings_at_other_levels_fail(self):
        for line in ("### [1.1.0]", "# [Unreleased]", " ## [1.1.0]", "## [1.1.0] ##"):
            with self.subTest(line=line):
                with self.assertRaises(SystemExit) as raised:
                    release_notes.newest_release(
                        f"{line}\n\n- New.\n\n## [1.0.0]\n\n- Old.\n"
                    )
                self.assertIn("level-two heading", str(raised.exception))

    def test_section_with_only_empty_subsections_fails(self):
        with self.assertRaises(SystemExit) as raised:
            release_notes.newest_release(
                "## [1.1.0]\n\n### Added\n\n### Fixed\n\n## [1.0.0]\n\n- Old.\n"
            )
        self.assertIn("has no release notes", str(raised.exception))

    def test_windows_line_endings_are_accepted(self):
        version, _, notes = release_notes.newest_release("## [1.1.0]\r\n\r\n- New.\r\n")
        self.assertEqual((version, notes), ("1.1.0", "- New.\n"))


class IntroducedTests(unittest.TestCase):
    def test_version_added_by_this_commit(self):
        self.assertTrue(
            release_notes.introduced(
                "1.1.0", "## [Unreleased]\n\n- A.\n\n## [1.0.0]\n\n- Old.\n"
            )
        )

    def test_version_already_in_the_parent(self):
        self.assertFalse(release_notes.introduced("1.1.0", "## [1.1.0]\n\n- A.\n"))

    def test_missing_or_unparseable_parent_counts_as_introduced(self):
        self.assertTrue(release_notes.introduced("1.0.0", ""))
        self.assertTrue(release_notes.introduced("1.0.0", "## [one]\n\n- A.\n"))


class MainTests(unittest.TestCase):
    def run_main(self, changelog, released="", previous=None):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
            (root / "released.txt").write_text(released, encoding="utf-8")
            args = [sys.executable, str(SCRIPT), "notes.md", "released.txt"]
            if previous is not None:
                (root / "previous.md").write_text(previous, encoding="utf-8")
                args.append("previous.md")
            env = {**os.environ, "GITHUB_OUTPUT": str(root / "output")}
            result = subprocess.run(
                args, cwd=root, env=env, text=True, capture_output=True, check=False
            )
            output = (
                (root / "output").read_text(encoding="utf-8")
                if (root / "output").exists()
                else ""
            )
            return result, output

    def test_outputs_version_major_and_introduced(self):
        result, output = self.run_main(
            "## [1.1.0]\n\n- New.\n", "v1.0.0\nv1\n", "## [1.0.0]\n\n- Old.\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "version=1.1.0\nmajor=v1\nintroduced=true\n")

    def test_lower_version_fails_before_any_output(self):
        result, output = self.run_main("## [0.9.0]\n\n- Old.\n", "v1.0.0\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")


class ReleaseOrderTests(unittest.TestCase):
    def test_lower_version_than_an_existing_release_fails(self):
        with self.assertRaises(SystemExit) as raised:
            release_notes.check_not_older("0.9.0", ["v1.0.0", "v1"])
        self.assertIn("lower than the newest release v1.0.0", str(raised.exception))

    def test_same_or_newer_version_passes(self):
        for version in ("1.0.0", "1.0.1", "2.0.0"):
            with self.subTest(version=version):
                release_notes.check_not_older(
                    version, ["v1.0.0", "v1", "not-a-release"]
                )

    def test_versions_compare_numerically(self):
        release_notes.check_not_older("1.10.0", ["v1.9.0"])
        with self.assertRaises(SystemExit):
            release_notes.check_not_older("1.9.0", ["v1.10.0"])

    def test_repository_changelog_parses(self):
        changelog = (SCRIPT.parents[2] / "CHANGELOG.md").read_text(encoding="utf-8")
        version, major, notes = release_notes.newest_release(changelog)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertTrue(major.startswith("v"))
        self.assertTrue(notes.strip())

"""Tests for the changelog parser that drives the release workflow."""

import collections
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - requirements-dev.txt installs PyYAML
    yaml = None  # type: ignore[assignment]

SCRIPT = Path(__file__).parents[1] / ".github/scripts/release_notes.py"
spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
assert spec and spec.loader
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)
ROOT = SCRIPT.parents[2]
UPGRADE_LINK = re.compile(r"\]\((?:\./)?UPGRADING\.md#([^)\s]*)\)")
COMMIT = "0123456789abcdef" * 2 + "01234567"
PINNING = "\n### Pinning this release\n"
WORKFLOW_CALL = re.compile(
    r"^ +uses: alex-papaioannou/python-ci-workflows/\.github/workflows/"
    r"python-ci\.yml@(.*)$",
    re.MULTILINE,
)
PINNED_RELEASE = re.compile(r"([0-9a-f]{40}) # v([0-9]+\.[0-9]+\.[0-9]+)")
REPOSITORY_URL = "https://github.com/alex-papaioannou/python-ci-workflows"


def readme_section(title):
    """Return the body of the README's level-two section with this title."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    return re.search(rf"(?ms)^## {title}\n(.*?)^## ", readme).group(1)


def yaml_blocks(markdown):
    """Return the contents of the YAML code blocks in a Markdown document."""
    return re.findall(r"(?ms)^```yaml\n(.*?)^```", markdown)


def git(*args):
    """Run git in the repository; return the result, or None if it cannot run."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            # The repository is public, so never wait for credentials.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def github_anchors(markdown):
    """Return the anchors GitHub gives the headings of a Markdown document."""
    anchors = set()
    seen = collections.Counter()
    for line, fenced in release_notes.fenced_lines(markdown):
        match = None if fenced else release_notes.ANY_HEADING.fullmatch(line)
        if not match:
            continue
        title = match.group(1).strip().lower()
        slug = re.sub(r"[^\w\- ]", "", title).replace(" ", "-")
        anchors.add(f"{slug}-{seen[slug]}" if seen[slug] else slug)
        seen[slug] += 1
    return anchors


def upgrade_link_problems(changelog, upgrading):
    """List Breaking changes entries without a link to an UPGRADING.md heading.

    Only the Unreleased section and the newest release section are checked.
    """
    checked = []
    for title, notes in release_notes.sections(changelog):
        checked.append((title, notes))
        if title.lower() != "[unreleased]":
            break
    anchors = github_anchors(upgrading)
    problems = []
    for title, notes in checked:
        for entry in release_notes.breaking_changes(notes):
            links = UPGRADE_LINK.findall(entry)
            if not links:
                problems.append(
                    f"Breaking changes entry in '## {title}' does not link to an "
                    f"UPGRADING.md heading: {entry}"
                )
            problems.extend(
                f"Breaking changes entry in '## {title}' links to UPGRADING.md#"
                f"{anchor}, which is not a heading in UPGRADING.md: {entry}"
                for anchor in links
                if anchor not in anchors
            )
    return problems


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
    def run_main(self, changelog, released="", previous=None, commit=COMMIT):
        """Run the script; return its result, its outputs and the notes it wrote."""
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
            (root / "released.txt").write_text(released, encoding="utf-8")
            args = [sys.executable, str(SCRIPT), "notes.md", "released.txt"]
            if previous is not None:
                (root / "previous.md").write_text(previous, encoding="utf-8")
                args.append("previous.md")
            env = {
                **os.environ,
                "GITHUB_OUTPUT": str(root / "output"),
                "GITHUB_SERVER_URL": "https://github.example",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_SHA": commit,
            }
            result = subprocess.run(
                args, cwd=root, env=env, text=True, capture_output=True, check=False
            )
            output, notes = (
                (root / name).read_text(encoding="utf-8")
                if (root / name).exists()
                else ""
                for name in ("output", "notes.md")
            )
            return result, output, notes

    def test_outputs_version_major_and_introduced(self):
        result, output, notes = self.run_main(
            "## [1.1.0]\n\n- New.\n", "v1.0.0\nv1\n", "## [1.0.0]\n\n- Old.\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "version=1.1.0\nmajor=v1\nintroduced=true\n")
        self.assertEqual(notes.partition(PINNING)[0], "- New.\n")

    def test_lower_version_fails_before_any_output(self):
        result, output, notes = self.run_main("## [0.9.0]\n\n- Old.\n", "v1.0.0\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((output, notes), ("", ""))

    def test_breaking_changes_need_a_new_major_version_before_any_output(self):
        for changelog, message in (
            (
                "## [1.2.0]\n\n### Breaking changes\n\n- Removed `a`.\n",
                "release it as 2.0.0",
            ),
            ("## [2.0.0]\n\n### Added\n\n- New.\n", "no 'Breaking changes'"),
        ):
            with self.subTest(changelog=changelog):
                result, output, notes = self.run_main(changelog, "v1.1.0\nv1\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual((output, notes), ("", ""))

    def test_new_major_version_with_breaking_changes_is_released(self):
        result, output, _ = self.run_main(
            "## [2.0.0]\n\n### Breaking changes\n\n- Removed `a`.\n", "v1.1.0\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "version=2.0.0\nmajor=v2\n")

    def test_unreleased_breaking_changes_block_a_new_release_before_any_output(self):
        # The release commit already contains whatever Unreleased lists, including
        # when a pending version is published later by a manual run from main.
        unreleased = "## [Unreleased]\n\n### Breaking changes\n\n- Removed `b`.\n\n"
        for released, message in (
            ("## [1.2.0]\n\n### Added\n\n- New.\n", "release it as 2.0.0"),
            (
                "## [2.0.0]\n\n### Breaking changes\n\n- Removed `a`.\n",
                "move them into the 2.0.0 section",
            ),
        ):
            with self.subTest(released=released):
                result, output, notes = self.run_main(
                    unreleased + released, "v1.1.0\nv1\n"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("'## [Unreleased]'", result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual((output, notes), ("", ""))

    def test_published_version_passes_with_unreleased_breaking_changes(self):
        result, output, _ = self.run_main(
            "## [Unreleased]\n\n### Breaking changes\n\n- Removed `b`.\n\n"
            "## [2.0.0]\n\n### Breaking changes\n\n- Removed `a`.\n",
            "v2.0.0\nv2\nv1.1.0\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "version=2.0.0\nmajor=v2\n")

    def test_upgrade_guide_links_point_at_the_released_tag(self):
        # GitHub resolves relative links in release notes against the release page.
        result, _, notes = self.run_main(
            "## [2.0.0]\n\n### Breaking changes\n\n"
            "- Removed `a`. See [UPGRADING.md](UPGRADING.md#a-is-removed).\n"
            "- Renamed `b`. See [the guide](./UPGRADING.md) and [c](OTHER.md#c).\n",
            "v1.1.0\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        guide = "https://github.example/owner/repo/blob/v2.0.0/UPGRADING.md"
        self.assertEqual(
            notes.partition(PINNING)[0],
            "### Breaking changes\n\n"
            f"- Removed `a`. See [UPGRADING.md]({guide}#a-is-removed).\n"
            f"- Renamed `b`. See [the guide]({guide}) and [c](OTHER.md#c).\n",
        )

    def test_notes_list_caller_impact_first_and_end_with_the_pinned_release(self):
        result, _, notes = self.run_main(
            "## [1.2.0]\n\n### Added\n\n- New.\n\n### Fixed\n\n- Bug.\n\n"
            "### Behaviour changes\n\n- Fails now.\n\n## [1.1.0]\n\n- Old.\n",
            "v1.1.0\nv1\nv1.0.0\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            notes,
            "### Behaviour changes\n\n- Fails now.\n\n### Added\n\n- New.\n\n"
            "### Fixed\n\n- Bug.\n\n### Pinning this release\n\n"
            f"This release is commit `{COMMIT}`. To pin a caller to it, use this "
            "line:\n\n```yaml\n"
            f"    uses: owner/repo/.github/workflows/python-ci.yml@{COMMIT} # v1.2.0\n"
            "```\n\n**Full changelog**: "
            "https://github.example/owner/repo/compare/v1.1.0...v1.2.0\n",
        )

    def test_compare_link_names_the_release_before_this_one(self):
        for released, link in (
            ("v1.1.0\nv1\nv1.0.0\n", "/compare/v1.1.0...v1.2.0\n"),
            # A re-run, after the release exists, still compares with the one before.
            ("v1.2.0\nv1\nv1.1.0\nv1.0.0\n", "/compare/v1.1.0...v1.2.0\n"),
            ("v1.1.10\nv1.1.9\n", "/compare/v1.1.10...v1.2.0\n"),
        ):
            with self.subTest(released=released):
                result, _, notes = self.run_main("## [1.2.0]\n\n- New.\n", released)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(notes.endswith(link), notes)
        # A patch release of an older major version compares within that version.
        self.assertEqual(
            release_notes.release_before("1.2.1", ["v2.0.0", "v1.2.0", "v1"]),
            (1, 2, 0),
        )

    def test_first_release_has_a_pinned_line_but_no_compare_link(self):
        for released in ("", "v1\nnot-a-release\n"):
            with self.subTest(released=released):
                result, _, notes = self.run_main("## [1.0.0]\n\n- First.\n", released)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"python-ci.yml@{COMMIT} # v1.0.0\n```\n", notes)
                self.assertNotIn("compare", notes)
                self.assertTrue(notes.endswith("```\n"), notes)

    def test_malformed_commit_fails_before_any_output(self):
        for commit in ("main", COMMIT[:12], COMMIT.upper(), ""):
            with self.subTest(commit=commit):
                result, output, notes = self.run_main(
                    "## [1.1.0]\n\n- New.\n", "v1.0.0\n", commit=commit
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("is not a full commit SHA", result.stderr)
                self.assertEqual((output, notes), ("", ""))


class ImpactFirstTests(unittest.TestCase):
    def test_caller_impact_subsections_come_first_in_a_fixed_order(self):
        notes = (
            "Intro.\n\n### Added\n\n- New.\n\n### Behaviour changes\n\n- Fails.\n\n"
            "### Security\n\n- Fix.\n\n### Changed defaults\n\n- Default.\n\n"
            "### Breaking changes\n\n- Removed.\n"
        )
        self.assertEqual(
            release_notes.impact_first(notes),
            "Intro.\n\n### Breaking changes\n\n- Removed.\n\n"
            "### Changed defaults\n\n- Default.\n\n### Behaviour changes\n\n"
            "- Fails.\n\n### Added\n\n- New.\n\n### Security\n\n- Fix.\n",
        )

    def test_deeper_headings_and_code_blocks_stay_in_their_subsection(self):
        notes = (
            "### Added\n\n- New:\n\n  ```md\n### Changed defaults\n  ```\n\n"
            "### CHANGED DEFAULTS\n\n#### Inputs\n\n- `a` is 2.\n"
        )
        self.assertEqual(
            release_notes.impact_first(notes),
            "### CHANGED DEFAULTS\n\n#### Inputs\n\n- `a` is 2.\n\n"
            "### Added\n\n- New:\n\n  ```md\n### Changed defaults\n  ```\n",
        )

    def test_notes_already_in_order_are_unchanged(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for notes in (
            "- Only.\n",
            "### Changed defaults\n\n- A.\n\n### Added\n\n- B.\n",
            release_notes.sections(changelog)[-1][1] + "\n",
        ):
            with self.subTest(notes=notes[:40]):
                self.assertEqual(release_notes.impact_first(notes), notes)


class BreakingChangesTests(unittest.TestCase):
    def test_entries_are_the_bullets_of_the_subsection(self):
        notes = (
            "### Breaking changes\n\n- Removed `a`.\n- Renamed `b` to `c`,\n"
            "  so callers must rename it.\n  - Nested detail.\n\n"
            "### Added\n\n- Not breaking.\n"
        )
        self.assertEqual(
            release_notes.breaking_changes(notes),
            [
                "- Removed `a`.",
                "- Renamed `b` to `c`, so callers must rename it. - Nested detail.",
            ],
        )

    def test_no_subsection_or_an_empty_one_has_no_entries(self):
        for notes in (
            "### Added\n\n- New.\n",
            "### Breaking changes\n\n### Added\n\n- New.\n",
            "",
        ):
            with self.subTest(notes=notes):
                self.assertEqual(release_notes.breaking_changes(notes), [])

    def test_ordered_list_items_are_separate_entries(self):
        notes = "### Breaking changes\n\n1. Removed `a`.\n2) Renamed `b`.\n   More.\n"
        self.assertEqual(
            release_notes.breaking_changes(notes),
            ["1. Removed `a`.", "2) Renamed `b`. More."],
        )

    def test_code_blocks_stay_in_their_entry(self):
        notes = (
            "### Breaking changes\n\n- Removed `a`. Before:\n\n  ```yaml\n"
            "  # old\n\n  a: 1\n  ```\n- Renamed `b`.\n  ~~~~\n## not a heading\n"
            "```\n  ~~~~\n\n### Added\n\n- New.\n"
        )
        self.assertEqual(
            release_notes.breaking_changes(notes),
            [
                "- Removed `a`. Before: ```yaml # old a: 1 ```",
                "- Renamed `b`. ~~~~ ## not a heading ``` ~~~~",
            ],
        )

    def test_paragraphs_deeper_headings_and_any_case_count(self):
        notes = (
            "### BREAKING CHANGES\r\n\r\nPython 3.11 is no longer supported.\r\n\r\n"
            "#### Inputs\r\n\r\n* Removed `a`.\r\n\r\n## Fixed\r\n\r\n- Bug.\r\n"
        )
        self.assertEqual(
            release_notes.breaking_changes(notes),
            ["Python 3.11 is no longer supported.", "* Removed `a`."],
        )


class UnreleasedTests(unittest.TestCase):
    PENDING = "## [Unreleased]\n\n### Breaking changes\n\n- Removed `b`.\n\n"

    def test_new_release_with_unreleased_breaking_changes_fails(self):
        for version, fix in (
            ("1.2.0", "move them into the 1.2.0 section and release it as 2.0.0"),
            ("2.0.0", "move them into the 2.0.0 section"),
        ):
            with self.subTest(version=version):
                changelog = self.PENDING + f"## [{version}]\n\n- New.\n"
                with self.assertRaises(SystemExit) as raised:
                    release_notes.check_unreleased(version, changelog, ["v1.1.0", "v1"])
                message = str(raised.exception)
                self.assertTrue(message.endswith(f"would ship in {version}; {fix}"))

    def test_published_version_or_no_pending_breaking_changes_pass(self):
        release_notes.check_unreleased(
            "2.0.0", self.PENDING + "## [2.0.0]\n\n- New.\n", ["v2.0.0", "v1.1.0"]
        )
        for unreleased in (
            "## [Unreleased]\n\n### Added\n\n- New.\n\n",
            "## [Unreleased]\n\n### Breaking changes\n\n",
            "",
        ):
            with self.subTest(unreleased=unreleased):
                release_notes.check_unreleased(
                    "1.2.0", unreleased + "## [1.2.0]\n\n- New.\n", ["v1.1.0"]
                )

    def test_breaking_changes_in_older_sections_are_not_pending(self):
        changelog = (
            "## [Unreleased]\n\n## [2.0.1]\n\n- Fix.\n\n"
            "## [2.0.0]\n\n### Breaking changes\n\n- Removed `a`.\n"
        )
        release_notes.check_unreleased("2.0.1", changelog, ["v2.0.0"])


class MajorVersionTests(unittest.TestCase):
    BREAKING = "### Breaking changes\n\n- Removed `a`.\n"
    ADDED = "### Added\n\n- New.\n"

    def test_breaking_changes_in_the_same_major_version_fail(self):
        for version in ("1.2.0", "1.1.1"):
            with self.subTest(version=version):
                with self.assertRaises(SystemExit) as raised:
                    release_notes.check_major_bump(
                        version, self.BREAKING, ["v1.1.0", "v1.0.0", "v1"]
                    )
                self.assertIn("'Breaking changes' entries", str(raised.exception))
                self.assertIn("release it as 2.0.0", str(raised.exception))

    def test_new_major_version_without_breaking_changes_fails(self):
        for notes in (self.ADDED, "### Breaking changes\n\n" + self.ADDED):
            with self.subTest(notes=notes):
                with self.assertRaises(SystemExit) as raised:
                    release_notes.check_major_bump("2.0.0", notes, ["v1.1.0"])
                self.assertIn(
                    "raises the major version from v1.1.0", str(raised.exception)
                )

    def test_major_version_rises_exactly_with_breaking_changes(self):
        release_notes.check_major_bump("2.0.0", self.BREAKING, ["v1.1.0", "v1"])
        release_notes.check_major_bump("1.2.0", self.ADDED, ["v1.1.0", "v1"])
        release_notes.check_major_bump("1.1.1", self.ADDED, ["v1.1.0"])

    def test_published_version_is_compared_with_the_release_before_it(self):
        # Every later push re-reads the published section; it must keep passing.
        release_notes.check_major_bump(
            "2.0.0", self.BREAKING, ["v2.0.0", "v2", "v1.1.0"]
        )
        release_notes.check_major_bump("2.0.1", self.ADDED, ["v2.0.1", "v2.0.0"])

    def test_first_release_has_nothing_to_compare(self):
        release_notes.check_major_bump("1.0.0", self.ADDED, [])
        release_notes.check_major_bump("2.0.0", self.ADDED, ["v2", "not-a-release"])

    def test_repository_changelog_matches_its_two_newest_releases(self):
        # The release job applies this rule against the newest release tag; applying
        # it to the two newest release sections fails a mismatched release pull
        # request.
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        releases = [
            (".".join(match.groups()), notes)
            for title, notes in release_notes.sections(changelog)
            if (match := release_notes.VERSION_HEADING.fullmatch(title))
        ]
        self.assertGreaterEqual(len(releases), 2)
        (version, notes), (previous, _) = releases[:2]
        release_notes.check_major_bump(version, notes, [f"v{previous}"])


class UpgradeGuideTests(unittest.TestCase):
    UPGRADING = "# Upgrading\n\n## From v1 to v2\n\n### `a` is removed\n\nBefore.\n"

    def changelog(self, entry, section="Unreleased"):
        return (
            f"# Changelog\n\n## [{section}]\n\n### Breaking changes\n\n{entry}\n\n"
            "### Added\n\n- New, without a link.\n\n## [1.0.0]\n\n- Old.\n"
        )

    def test_entry_without_a_link_fails(self):
        for entry in (
            "- Removed `a`.",
            "- Removed `a`; see [the guide](UPGRADING.md).",
            "- Removed `a`; see [the guide](OTHER.md#a-is-removed).",
        ):
            with self.subTest(entry=entry):
                problems = upgrade_link_problems(self.changelog(entry), self.UPGRADING)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("does not link to an UPGRADING.md heading", problems[0])
                self.assertIn("'## [Unreleased]'", problems[0])

    def test_link_to_a_missing_heading_fails(self):
        entry = "- Removed `a`. See [UPGRADING.md](UPGRADING.md#a-was-removed)."
        problems = upgrade_link_problems(
            self.changelog(entry, "2.0.0 - 2026-11-01"), self.UPGRADING
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("UPGRADING.md#a-was-removed, which is not a heading", problems[0])

    def test_entry_linking_an_existing_heading_passes(self):
        entry = (
            "- Removed `a`. See [how to\n  upgrade](UPGRADING.md#a-is-removed) or\n"
            "  [the overview](./UPGRADING.md#from-v1-to-v2)."
        )
        self.assertEqual(
            upgrade_link_problems(self.changelog(entry), self.UPGRADING), []
        )

    def test_only_unreleased_and_the_newest_release_are_checked(self):
        older = "\n\n## [0.9.0]\n\n### Breaking changes\n\n- Unlinked.\n"
        changelog = (
            "## [Unreleased]\n\n## [1.0.0]\n\n### Breaking changes\n\n- Unlinked.\n"
        )
        problems = upgrade_link_problems(changelog + older, self.UPGRADING)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("'## [1.0.0]'", problems[0])

    def test_lines_in_code_blocks_are_not_headings(self):
        upgrading = (
            self.UPGRADING + "\n```yaml\n# Before\nwith:\n  a: 1\n```\n\n"
            "    ### Indented code\n"
        )
        self.assertEqual(
            github_anchors(upgrading), {"upgrading", "from-v1-to-v2", "a-is-removed"}
        )
        entry = "- Removed `a`. See [UPGRADING.md](UPGRADING.md#before)."
        problems = upgrade_link_problems(self.changelog(entry), upgrading)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("UPGRADING.md#before, which is not a heading", problems[0])

    def test_every_list_item_and_entry_after_a_code_block_is_checked(self):
        for entry in (
            "1. Removed `a`. [x](UPGRADING.md#a-is-removed)\n2. Renamed `b`.",
            (
                "- Removed `a`. [x](UPGRADING.md#a-is-removed)\n\n  ```yaml\n"
                "  # Before\n  ```\n- Renamed `b`."
            ),
        ):
            with self.subTest(entry=entry):
                problems = upgrade_link_problems(self.changelog(entry), self.UPGRADING)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("does not link", problems[0])
                self.assertIn("Renamed `b`.", problems[0])

    def test_anchors_follow_github_heading_ids(self):
        anchors = github_anchors(
            "# Upgrading\n## From v1 to v2\n### `max-parallel` defaults to 256!\n"
            "### Removed inputs\n### Removed inputs\n"
        )
        self.assertEqual(
            anchors,
            {
                "upgrading",
                "from-v1-to-v2",
                "max-parallel-defaults-to-256",
                "removed-inputs",
                "removed-inputs-1",
            },
        )

    def test_repository_breaking_changes_link_the_upgrade_guide(self):
        upgrading = (ROOT / "UPGRADING.md").read_text(encoding="utf-8")
        self.assertIn("from-v1-to-v2", github_anchors(upgrading))
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(upgrade_link_problems(changelog, upgrading), [])


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

    def test_repository_changelog_collects_changes_under_unreleased(self):
        changelog = (SCRIPT.parents[2] / "CHANGELOG.md").read_text(encoding="utf-8")
        first_section = re.search(r"(?m)^## (.*)$", changelog).group(1)
        self.assertEqual(first_section, "[Unreleased]")

    def test_repository_unreleased_section_keeps_every_subsection(self):
        contributing = " ".join(
            (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").split()
        )
        listed = re.search(r"in one of these subsections: ([^.]*)\.", contributing)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        title, unreleased = release_notes.sections(changelog)[0]
        self.assertEqual(title, "[Unreleased]")
        self.assertEqual(
            re.findall(r"(?m)^### (.*)$", unreleased), listed.group(1).split(", ")
        )
        for subsection in ("Breaking changes", "Changed defaults", "Behaviour changes"):
            self.assertIn(subsection, listed.group(1))

    def test_repository_releases_have_no_empty_subsections(self):
        # A release section is published as it is, so a template heading that the
        # release pull request left empty would end up in the release notes.
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for title, notes in release_notes.sections(changelog):
            if title.lower() == "[unreleased]":
                continue
            parts = re.split(r"(?m)^(###[ \t].*)$", notes)
            for heading, body in zip(parts[1::2], parts[2::2]):
                with self.subTest(section=title, subsection=heading):
                    self.assertTrue(body.strip(), f"'## {title}' has empty {heading!r}")

    def test_repository_changelog_parses(self):
        changelog = (SCRIPT.parents[2] / "CHANGELOG.md").read_text(encoding="utf-8")
        version, major, notes = release_notes.newest_release(changelog)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertTrue(major.startswith("v"))
        self.assertTrue(notes.strip())


class ReadmePinTests(unittest.TestCase):
    def quick_start_pin(self):
        """Return (ref, commit, version) of the quick start's first snippet."""
        first = yaml_blocks(readme_section("Quick start"))[0]
        (ref,) = WORKFLOW_CALL.findall(first)
        pinned = PINNED_RELEASE.fullmatch(ref)
        self.assertIsNotNone(pinned, f"not a full SHA with a '# vX.Y.Z' comment: {ref}")
        return ref, pinned.group(1), pinned.group(2)

    def test_quick_start_pins_one_of_the_two_newest_releases(self):
        # The README can name a release only after it exists, so the pull request
        # that updates the pin follows the release; until then it names the one
        # before.
        _, _, version = self.quick_start_pin()
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        releases = [
            ".".join(match.groups())
            for title, _ in release_notes.sections(changelog)
            if (match := release_notes.VERSION_HEADING.fullmatch(title))
        ]
        self.assertIn(version, releases[:2])

    def test_every_readme_call_uses_the_pin_or_the_major_tag(self):
        ref, _, version = self.quick_start_pin()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        major = "v" + version.split(".")[0]
        calls = WORKFLOW_CALL.findall(readme)
        self.assertEqual(set(calls), {ref, major})
        # The major tag is only the quick start's convenience option.
        self.assertEqual(calls.count(major), 1)

    def test_quick_start_pin_is_the_release_commit(self):
        # CI checks out one commit without tags, so the tag is looked up on GitHub
        # when it is not fetched, and a mistyped SHA in the pull request that
        # updates the pin fails CI. The test skips only when GitHub is unreachable.
        _, commit, version = self.quick_start_pin()
        tag = f"refs/tags/v{version}"
        local = git("rev-parse", "--verify", "--quiet", tag + "^{commit}")
        if local and local.returncode == 0:
            self.assertEqual(local.stdout.strip(), commit)
            return
        # --exit-code exits with 2 when there is no such tag, and with another
        # status when GitHub cannot be reached. An annotated tag's commit is the
        # line for "<tag>^{}".
        remote = git("ls-remote", "--exit-code", REPOSITORY_URL, tag, tag + "^{}")
        if remote is None or remote.returncode not in (0, 2):
            reason = remote.stderr.strip() if remote else "git did not run"
            self.skipTest(f"cannot list the tags of {REPOSITORY_URL}: {reason}")
        refs = {
            name: sha
            for sha, name in (line.split("\t") for line in remote.stdout.splitlines())
        }
        self.assertIn(tag, refs, f"{REPOSITORY_URL} has no tag v{version}")
        self.assertEqual(refs.get(tag + "^{}", refs[tag]), commit)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_dependabot_configuration_groups_updates_with_a_cooldown(self):
        (block,) = [
            block
            for block in yaml_blocks(readme_section("Quick start"))
            if "package-ecosystem" in block
        ]
        config = yaml.safe_load(block)
        self.assertEqual(config["version"], 2)
        (update,) = config["updates"]
        self.assertEqual(update["package-ecosystem"], "github-actions")
        self.assertEqual(update["directory"], "/")
        self.assertGreater(update["cooldown"]["default-days"], 0)
        # Every update is grouped except a new major version, which gets its own
        # pull request.
        (group,) = update["groups"].values()
        self.assertEqual(group["patterns"], ["*"])
        self.assertEqual(sorted(group["update-types"]), ["minor", "patch"])

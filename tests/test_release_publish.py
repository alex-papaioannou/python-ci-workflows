"""Run release.yml's publish step against a fake gh that keeps tags in a file."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - requirements-dev.txt installs PyYAML
    yaml = None  # type: ignore[assignment]

RELEASE = Path(__file__).parents[1] / ".github/workflows/release.yml"
NEW, OLD = "a" * 40, "b" * 40

FAKE_GH = textwrap.dedent(
    """\
    import json, os, sys

    path = os.environ["FAKE_GH_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    state["calls"].append(args)

    def save():
        json.dump(state, open(path, "w"))

    def option(name):
        return args[args.index(name) + 1]

    def fields():
        pairs = [args[i + 1] for i, a in enumerate(args) if a in ("-f", "-F")]
        return dict(p.split("=", 1) for p in pairs)

    if state.get("outage") and args[0] == "api":
        # Tag lookups fail with a server error; other commands still work, so a
        # step that mistook the error for a missing tag would go on to release.
        save()
        sys.exit("gh: Server Error (HTTP 500)")
    if args[:2] == ["release", "view"]:
        save()
        sys.exit(0 if args[2] in state["releases"] else 1)
    if args[:2] == ["release", "create"]:
        tag = args[2]
        state["releases"].append(tag)
        state["tags"].setdefault(tag, {"type": "commit", "sha": option("--target")})
        save()
        sys.exit(0)
    if args[0] == "api":
        path_arg = next(a for a in args[1:] if a.startswith("repos/"))
        name = path_arg.rsplit("/", 1)[1]
        if "/git/ref/tags/" in path_arg:
            tag = state["tags"].get(name)
            save()
            if tag is None:
                sys.exit("gh: Not Found (HTTP 404)")
            print(tag["type"] + " " + tag["sha"])
            sys.exit(0)
        if "/git/tags/" in path_arg:
            print(state["annotated"][name])
            save()
            sys.exit(0)
        if "/git/refs/tags/" in path_arg:
            state["tags"][name] = {"type": "commit", "sha": fields()["sha"]}
            save()
            sys.exit(0)
        if path_arg.endswith("/git/refs"):
            ref = fields()["ref"].rsplit("/", 1)[1]
            state["tags"][ref] = {"type": "commit", "sha": fields()["sha"]}
            save()
            sys.exit(0)
    save()
    sys.exit(f"fake gh: unsupported {args}")
    """
)


def publish_script():
    steps = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))["jobs"]["release"][
        "steps"
    ]
    return next(
        s["run"]
        for s in steps
        if s.get("name") == "Publish the release and move the major tag"
    )


@unittest.skipUnless(
    yaml and shutil.which("bash") and os.name != "nt", "needs PyYAML and a POSIX bash"
)
class PublishStepTests(unittest.TestCase):
    def run_step(
        self, tags, releases, introduced="true", event="push", version="1.1.0", **extra
    ):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        bin_dir = work / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
        gh.chmod(0o755)
        state = work / "state.json"
        state.write_text(
            json.dumps({"tags": tags, "releases": releases, "calls": [], **extra})
        )
        (work / "notes.md").write_text("- Notes.\n")
        script = work / "step.sh"
        script.write_text(publish_script(), encoding="utf-8")
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_STATE": str(state),
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_SHA": NEW,
            "RUNNER_TEMP": str(work),
            "GITHUB_STEP_SUMMARY": str(work / "summary.md"),
            "VERSION": version,
            "MAJOR": "v" + version.split(".")[0],
            "INTRODUCED": introduced,
            "EVENT": event,
        }
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        final = json.loads(state.read_text())
        summary = (
            (work / "summary.md").read_text() if (work / "summary.md").exists() else ""
        )
        return result, final, summary

    def test_new_release_is_created_and_major_tag_moved(self):
        result, state, summary = self.run_step(
            {
                "v1.0.0": {"type": "commit", "sha": OLD},
                "v1": {"type": "commit", "sha": OLD},
            },
            ["v1.0.0"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("v1.1.0", state["releases"])
        self.assertEqual(state["tags"]["v1.1.0"]["sha"], NEW)
        self.assertEqual(state["tags"]["v1"]["sha"], NEW)
        self.assertIn("Released v1.1.0", summary)
        self.assertIn("::notice::Moved v1", result.stdout)

    def test_first_release_of_a_major_creates_its_tag(self):
        result, state, _ = self.run_step({}, [], version="2.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["tags"]["v2"]["sha"], NEW)

    def test_released_version_with_current_major_tag_changes_nothing(self):
        tags = {
            "v1.1.0": {"type": "commit", "sha": OLD},
            "v1": {"type": "commit", "sha": OLD},
        }
        result, state, summary = self.run_step(tags, ["v1.1.0"], introduced="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["tags"]["v1"]["sha"], OLD)
        self.assertIn("No new release", summary)
        self.assertNotIn("::notice::", result.stdout)

    def test_push_leaves_a_rolled_back_major_tag_alone(self):
        tags = {
            "v1.1.0": {"type": "commit", "sha": OLD},
            "v1": {"type": "commit", "sha": "c" * 40},
        }
        result, state, _ = self.run_step(tags, ["v1.1.0"], introduced="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["tags"]["v1"]["sha"], "c" * 40)
        self.assertIn("::warning::v1 points at", result.stdout)

    def test_manual_run_repairs_a_stale_major_tag(self):
        tags = {
            "v1.1.0": {"type": "commit", "sha": OLD},
            "v1": {"type": "commit", "sha": "c" * 40},
        }
        result, state, _ = self.run_step(
            tags, ["v1.1.0"], introduced="false", event="workflow_dispatch"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["tags"]["v1"]["sha"], OLD)

    def test_rerun_of_the_releasing_commit_repairs_the_major_tag(self):
        tags = {
            "v1.1.0": {"type": "commit", "sha": NEW},
            "v1": {"type": "commit", "sha": OLD},
        }
        result, state, _ = self.run_step(tags, ["v1.1.0"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["tags"]["v1"]["sha"], NEW)

    def test_annotated_tags_resolve_to_their_commit(self):
        tags = {
            "v1.1.0": {"type": "tag", "sha": "d" * 40},
            "v1": {"type": "commit", "sha": OLD},
        }
        result, state, _ = self.run_step(
            tags, ["v1.1.0"], event="workflow_dispatch", annotated={"d" * 40: NEW}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["tags"]["v1"]["sha"], NEW)

    def test_version_not_introduced_by_this_commit_is_refused(self):
        result, state, _ = self.run_step(
            {"v1": {"type": "commit", "sha": OLD}}, [], introduced="false"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not add it to CHANGELOG.md", result.stdout)
        self.assertEqual(state["releases"], [])

    def test_existing_tag_at_another_commit_is_refused(self):
        result, state, _ = self.run_step({"v1.1.0": {"type": "commit", "sha": OLD}}, [])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists at", result.stdout)
        self.assertEqual(state["releases"], [])

    def test_api_outage_fails_without_writing(self):
        result, state, _ = self.run_step({}, [], outage=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["releases"], [])
        self.assertEqual(state["tags"], {})


if __name__ == "__main__":
    unittest.main()

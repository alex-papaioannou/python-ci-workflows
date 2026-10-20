"""Check that every workflow step passes the environment its subcommand reads."""

import ast
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - requirements-dev.txt installs PyYAML
    yaml = None  # type: ignore[assignment]

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/python-ci.yml"
STEPS = ROOT / ".github/actions/python-ci/steps.py"

# Functions whose first argument names an environment variable.
ENV_READERS = {
    "env",
    "flag",
    "json_paths",
    "json_list",
    "validate_path_list",
    "check_directory",
    "check_version",
}
# Set by the runner or by the composite action, not by the step's env block.
PROVIDED = ("GITHUB_", "RUNNER_")
JOB_LEVEL = {"PYTHON_CI_STEPS"}


def steps_reads():
    """Map each steps.py subcommand to the environment variables it can read."""
    tree = ast.parse(STEPS.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    commands = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "STEPS" for t in node.targets)
    )

    def reads(name, seen):
        if name in seen or name not in functions:
            return set()
        seen.add(name)
        found = set()
        for node in ast.walk(functions[name]):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func.id if isinstance(node.func, ast.Name) else None
            if callee in ENV_READERS and node.args:
                argument = node.args[0]
                if not (
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                ):
                    raise AssertionError(
                        f"{name}() reads the environment through a non-literal name at line {node.lineno}"
                    )
                found.add(argument.value)
            if callee and callee not in ENV_READERS:
                # A reader's own body forwards its argument; the call site names it.
                found |= reads(callee, seen)
        return found

    return {
        key.value: reads(value.id, set())
        for key, value in zip(commands.keys, commands.values)
        if isinstance(key, ast.Constant) and isinstance(value, ast.Name)
    }


def workflow_steps():
    """Yield (job, step name, subcommand, env keys) for steps that run steps.py."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job_name, job in workflow["jobs"].items():
        job_env = set(job.get("env", {}))
        for step in job.get("steps", []):
            run = step.get("run", "")
            if run.startswith('python "$PYTHON_CI_STEPS" '):
                yield (
                    job_name,
                    step["name"],
                    run.split()[-1],
                    set(step.get("env", {})) | job_env,
                )


@unittest.skipUnless(yaml, "needs PyYAML")
class EnvContractTests(unittest.TestCase):
    def test_every_variable_a_step_reads_is_mapped(self):
        reads = steps_reads()
        for job, name, command, mapped in workflow_steps():
            with self.subTest(step=name):
                missing = {
                    variable
                    for variable in reads[command] - mapped - JOB_LEVEL
                    if not variable.startswith(PROVIDED)
                }
                self.assertEqual(
                    missing,
                    set(),
                    f"{job} / {name} runs '{command}' but does not map {sorted(missing)}",
                )

    def test_every_mapped_variable_is_read(self):
        reads = steps_reads()
        for job, name, command, mapped in workflow_steps():
            with self.subTest(step=name):
                unused = mapped - reads[command] - {"PYTHONWARNINGS", "PYTHONHASHSEED"}
                self.assertEqual(
                    unused,
                    set(),
                    f"{job} / {name} maps {sorted(unused)}, which '{command}' never reads",
                )

    def test_every_subcommand_is_used_by_the_workflow(self):
        used = {command for _, _, command, _ in workflow_steps()}
        self.assertEqual(set(steps_reads()) - used, set())


if __name__ == "__main__":
    unittest.main()

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


def is_os_environ(node):
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def literal(node):
    """Return the string a node spells out, or None if it is not a string literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def reads_environment(call):
    """Return whether a call reads one environment variable by name."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in ENV_READERS
    if isinstance(func, ast.Attribute):
        if func.attr == "get":
            return is_os_environ(func.value)
        return (
            func.attr == "getenv"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        )
    return False


def local_names(definition):
    """Return the names a function or class binds itself, which shadow module names."""
    names = set()
    for node in ast.walk(definition):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif node is not definition and isinstance(
            node, (ast.FunctionDef, ast.ClassDef)
        ):
            names.add(node.name)
    return names


def steps_reads(source=None):
    """Map each steps.py subcommand to the environment variables it can read.

    A subcommand reads what its function reads, directly or through any function or
    class it calls or refers to, plus whatever module-level code reads. Variable names
    must be string literals, except where a reader forwards its own parameter.
    """
    tree = ast.parse(source or STEPS.read_text(encoding="utf-8"))
    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    commands = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "STEPS" for t in node.targets)
    )

    def collect(roots, owner, forwarded, shadowed, seen):
        found = set()
        called = set()
        nodes = [node for root in roots for node in ast.walk(root)]
        for node in nodes:
            if isinstance(node, ast.Call):
                called.add(id(node.func))
                if not reads_environment(node):
                    continue
                if node.args:
                    argument = node.args[0]
                else:
                    keywords = [k.value for k in node.keywords if k.arg != "default"]
                    if not keywords:
                        raise AssertionError(
                            f"{owner} reads the environment without a name at line "
                            f"{node.lineno}"
                        )
                    argument = keywords[0]
                name = literal(argument)
                if name is not None:
                    found.add(name)
                elif not (isinstance(argument, ast.Name) and argument.id in forwarded):
                    raise AssertionError(
                        f"{owner} reads the environment through a non-literal name "
                        f"at line {node.lineno}"
                    )
            elif (
                isinstance(node, ast.Subscript)
                and is_os_environ(node.value)
                and isinstance(node.ctx, ast.Load)
            ):
                name = literal(node.slice)
                if name is None:
                    raise AssertionError(
                        f"{owner} indexes os.environ with a non-literal name at line "
                        f"{node.lineno}"
                    )
                found.add(name)
        for node in nodes:
            if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
                continue
            if node.id in shadowed:
                continue
            if node.id in ENV_READERS and id(node) not in called:
                raise AssertionError(
                    f"{owner} passes the reader {node.id}() around at line "
                    f"{node.lineno}; call it with a literal name instead"
                )
            found |= reads(node.id, seen)
        return found

    def reads(name, seen):
        if name in seen or name not in definitions:
            return set()
        seen.add(name)
        definition = definitions[name]
        forwarded = set()
        if name in ENV_READERS and isinstance(definition, ast.FunctionDef):
            # Only the first parameter names a variable; see ENV_READERS.
            forwarded = {definition.args.args[0].arg}
        return collect(
            [definition], f"{name}()", forwarded, local_names(definition), seen
        )

    # Code outside definitions, the __main__ block included, runs for every subcommand.
    module_code = [
        node
        for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.ClassDef))
        and not (isinstance(node, ast.Assign) and node.value is commands)
    ]
    module_reads = collect(module_code, "module-level code", set(), set(), set())
    return {
        key.value: reads(value.id, set()) | module_reads
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


PRELUDE = """
import atexit
import os


def env(name, default=""):
    return os.environ.get(name, default)


def flag(name):
    return env(name) == "true"


def check_version(variable):
    flag("FROM_READER_BODY")
    return env(variable)


def other():
    return env("OTHER_ONLY")
"""


class WalkerTests(unittest.TestCase):
    """steps_reads finds a read however steps.py spells it."""

    def reads(self, code):
        source = PRELUDE + code + '\nSTEPS = {"first": first, "other": other}\n'
        return steps_reads(source)

    def test_reads_are_found_in_every_form(self):
        cases = {
            "DIRECT": 'def first():\n    env("DIRECT")\n',
            "KEYWORD": 'def first():\n    env(name="KEYWORD")\n',
            "ENVIRON_GET": 'def first():\n    os.environ.get("ENVIRON_GET")\n',
            "GETENV": 'def first():\n    os.getenv("GETENV")\n',
            "SUBSCRIPT": 'def first():\n    os.environ["SUBSCRIPT"]\n',
            "FROM_READER_BODY": 'def first():\n    check_version("X")\n',
            "METHOD": (
                "class Result:\n    def start(self):\n        env('METHOD')\n\n\n"
                "def first():\n    run(resultclass=Result)\n"
            ),
            "STATIC": (
                "class Helpers:\n    @staticmethod\n    def limit():\n"
                "        return env('STATIC')\n\n\n"
                "def first():\n    Helpers.limit()\n"
            ),
            "BY_REFERENCE": (
                "def late():\n    env('BY_REFERENCE')\n\n\n"
                "def first():\n    atexit.register(late)\n"
            ),
            "MODULE_LEVEL": (
                "LIMIT = int(env('MODULE_LEVEL') or 10)\n\n\ndef first():\n    pass\n"
            ),
            "IN_MAIN": (
                "def main():\n    flag('IN_MAIN')\n\n\ndef first():\n    pass\n\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
        }
        for variable, code in cases.items():
            with self.subTest(variable=variable):
                self.assertIn(variable, self.reads(code)["first"])
        self.assertIn("MODULE_LEVEL", self.reads(cases["MODULE_LEVEL"])["other"])

    def test_local_names_do_not_pull_in_module_functions(self):
        code = "def first():\n    other = []\n    print(other)\n"
        self.assertNotIn("OTHER_ONLY", self.reads(code)["first"])

    def test_unverifiable_reads_are_rejected(self):
        cases = {
            "non-literal": "def first():\n    name = 'X'\n    env(name)\n",
            "non-literal subscript": "def first(key):\n    os.environ[key]\n",
            "reader passed around": "def first():\n    get = env\n    get('X')\n",
            "second parameter read": (
                "def check_version(variable, other):\n    return env(variable) + env(other)\n\n\n"
                "def first():\n    check_version('A', 'B')\n"
            ),
        }
        for case, code in cases.items():
            with self.subTest(case=case), self.assertRaises(AssertionError):
                self.reads(code)


if __name__ == "__main__":
    unittest.main()

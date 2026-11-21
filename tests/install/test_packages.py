"""
Tests for the end of the setup wizard (issue #19, boxes b, c and d): the install step, the index
step, "Next steps" and the Python that the MCP example names.

What they pin down:

- the wizard never starts the indexer with a Python that lacks Dewey's packages, and says what a
  skipped or failed install means (b);
- "Index them now?" stays No by default, with the reason on the line above it, and "Next steps"
  lists the indexing command whenever indexing did not finish in this run (c);
- the MCP example names a Python that has Dewey's packages, or says to change `command:` (d).

One function, install.modules_missing(), asks a fresh copy of a Python which modules it cannot
find. Most tests replace it with a described answer, so they need no particular packages;
ProbeTests runs the real one, once against a throwaway virtual environment. No terminal is
needed: input() is patched. Standard library only; no network.
tests/install/test_install.py runs four of these cases through a real terminal.
"""
import ast
import contextlib
import importlib.util
import io
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib import metadata
from unittest import mock

from tests.docs.test_docs import shell_problems
from tests.install import test_python_check as version_check_tests

REPO = pathlib.Path(__file__).resolve().parents[2]


def load_install():
    spec = importlib.util.spec_from_file_location("dewey_install_packages", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


install = load_install()

CLAUDE_VOYAGE = {"DEWEY_STACK": "anthropic", "EMBEDDING_PROVIDER": "voyage", "RERANK_PROVIDER": "voyage"}
CLAUDE_LOCAL_RERANKER = {"DEWEY_STACK": "anthropic", "EMBEDDING_PROVIDER": "voyage", "RERANK_PROVIDER": "local"}
OPEN_LOCAL = {"DEWEY_STACK": "open", "EMBEDDING_PROVIDER": "local", "RERANK_PROVIDER": "none"}
AZURE = {"DEWEY_STACK": "azure"}
LOCAL_MODEL_MODULES = ["torch", "sentence_transformers"]
EVERYTHING = "everything"   # for probe(): a Python that has none of the modules it is asked about

NEXT_STEPS_POINTER = '"Next steps", at the end of this wizard, says what to do.'
# Characters a shell acts on, and words that are also commands (a Mac's zsh runs `Make` as make):
# the lists the version check's lines are held to. Imported as a module, so its tests run once.
DANGEROUS = version_check_tests.PrintedCommandsPasteSafelyTests.DANGEROUS
ALSO_COMMANDS = version_check_tests.PrintedCommandsPasteSafelyTests.ALSO_COMMANDS


def probe(here=(), venv=()):
    """Replace install.modules_missing() with a described answer. `here` is what the Python running
    the wizard lacks and `venv` what the Dewey folder's .venv Python lacks: module names,
    EVERYTHING, or None for "that Python could not be asked"."""
    def modules_missing(modules, python=None):
        lacks = here if python is None else venv
        if lacks is None:
            return None
        return [name for name in modules if lacks == EVERYTHING or name in lacks]

    # create=True: on a version of install.py without the check, a test then fails on what the
    # wizard did (it started the indexer), not on the missing name.
    return mock.patch.object(install, "modules_missing", side_effect=modules_missing, create=True)


def computer(lives="none", system="mac", in_root=True):
    """A described computer. `lives` says where the Python running the wizard is: "none" (outside
    any environment, and the Dewey folder has no .venv), "off" (outside, and the Dewey folder has
    a .venv that is not turned on), "broken" (outside, and its .venv is not a working
    environment), "dewey" (in the Dewey folder's .venv), "other" (in an environment in another
    folder) or "other+own" (the same, and the Dewey folder has a working .venv as well)."""
    root = {"mac": "/Users/you/dewey-ai", "linux": "/home/you/dewey-ai", "windows": "C:\\Users\\you\\dewey-ai"}[system]
    sep = "\\" if system == "windows" else "/"
    venv = {"dewey": root + sep + ".venv", "other": "/somewhere/else/env", "other+own": "/somewhere/else/env"}
    return install.PythonSituation(system=system, root=root, in_root=in_root, venv=venv.get(lives, ""),
                                   in_dewey_venv=lives == "dewey",
                                   dewey_venv_exists=lives in ("off", "dewey", "other+own"),
                                   dewey_venv_broken=lives == "broken")


def wizard(function, *args, answers=(), **kwargs):
    """(result, printed text, questions asked) of a wizard function, with `answers` typed in order.
    A question nobody answered is an error, and so is an answer nobody asked for."""
    waiting, asked = list(answers), []

    def answer(prompt=""):
        asked.append(prompt)
        if not waiting:
            raise AssertionError(f"unexpected question: {prompt!r}")
        return waiting.pop(0)

    out = io.StringIO()
    with mock.patch.object(install, "_STYLED", False), contextlib.redirect_stdout(out), \
            mock.patch("builtins.input", answer), mock.patch.object(install.getpass, "getpass", answer):
        result = function(*args, **kwargs)
    if waiting:
        raise AssertionError(f"never asked for: {waiting!r}")
    return result, out.getvalue(), asked


def flat(text):
    """Text with its line breaks and indentation taken out, so that a test pins the wording of a
    note and not where its lines happen to wrap."""
    return " ".join(text.split())


def words(step):
    """A step's words: its lines that are not commands, as one line of text."""
    return flat(" ".join(line for line in step if not line.startswith("  ")))


def commands(lines):
    """The command lines among a step's lines: the ones that start with two spaces."""
    return [line.strip() for line in lines if line.startswith("  ")]


def assert_commands_stand_alone(test, text):
    """No printed line puts words after a command: `python install.py, or run:` pasted into a
    terminal runs `python install.py,`."""
    for line in text.splitlines():
        if re.search(r"python install\.py|pip install|app/\w+\.py", line) and not line.lstrip().startswith("$ "):
            test.assertRegex(line, r"^ +(python|py) [\w./ -]+$", f"words share a line with a command: {line!r}")
            test.assertEqual(shell_problems(line), [], line)


def make_environment(folder, modules=()):
    """A throwaway virtual environment in `folder`, made with `python -m venv --without-pip` (no
    network; a fraction of a second), holding an empty package for each name in `modules`.
    Returns the path of its Python, built the way the wizard builds the path of .venv's Python.

    If the environment cannot be made, the test FAILS with venv's own message: a skip would hide
    that in CI, and with it every test that needs a Python without Dewey's packages."""
    folder = pathlib.Path(folder)
    made = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(folder)], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=300)
    if made.returncode != 0:
        raise AssertionError(f"could not make a virtual environment in {folder}:\n{made.stdout}{made.stderr}")
    python = folder / install.venv_python().relative_to(install.ROOT / ".venv")
    asked = subprocess.run([str(python), "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    if asked.returncode != 0 or not asked.stdout.strip():
        raise AssertionError(f"the new environment's Python did not run ({python}):\n{asked.stdout}{asked.stderr}")
    site_packages = pathlib.Path(asked.stdout.strip())
    for name in modules:
        package = site_packages.joinpath(*name.split("."))
        package.mkdir(parents=True, exist_ok=True)
        # The file is what makes it count: a folder that only has the right name is not a package
        # the wizard believes in (ProbeTests).
        (package / "__init__.py").write_text("", encoding="utf-8")
    return python


def requirements_txt_is_missing_here():
    """Why this Python can't be said to have requirements.txt installed, or "" when it has: every
    pin without a marker has to be there (pins with markers are for other systems or Pythons)."""
    for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ";" in line:
            continue
        name = line.split("==")[0].split("[")[0]
        try:
            metadata.distribution(name)
        except metadata.PackageNotFoundError:
            return f"requirements.txt is not installed in this Python ({name} is missing)"
    return ""


class TempRootTestCase(unittest.TestCase):
    """install.ROOT pointed at an empty temporary Dewey folder with a data/ folder in it."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="dewey-packages-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "data").mkdir()
        for name, value in (("ROOT", self.root), ("ENV_PATH", self.root / ".env")):
            patch = mock.patch.object(install, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def articles(self, count=1):
        for number in range(count):
            (self.root / "data" / f"articles-{number:04d}.json").write_text("[]", encoding="utf-8")


# ----------------------------------------------------------------- the probe

class ProbeTests(unittest.TestCase):
    """modules_missing() itself, with real Pythons."""

    def folder(self):
        folder = pathlib.Path(tempfile.mkdtemp(prefix="dewey-probe-")).resolve()
        self.addCleanup(shutil.rmtree, folder, True)
        return folder

    def test_a_fresh_copy_of_this_python_is_asked_and_nothing_is_imported_here(self):
        asked = ["json", "dewey_no_such_module", "dewey_no_such_parent.child", "os.path"]
        self.assertEqual(install.modules_missing(asked), ["dewey_no_such_module", "dewey_no_such_parent.child"])
        self.assertEqual(install.modules_missing(["json"]), [])
        self.assertEqual(install.modules_missing([]), [])
        self.assertNotIn("dewey_no_such_module", sys.modules)

    def test_only_a_real_package_counts_and_the_dewey_folder_is_not_searched(self):
        # The probe runs in the Dewey folder, where `data` and `docs` are folders too: Python would
        # find them as modules from there. And a leftover empty folder in site-packages is not an
        # installed package: `import` finds it, `from it import anything` fails.
        folder = self.folder()
        (folder / "dewey_only_a_folder").mkdir()
        (folder / "dewey_a_package").mkdir()
        (folder / "dewey_a_package" / "__init__.py").write_text("", encoding="utf-8")
        (folder / "dewey_a_module.py").write_text("", encoding="utf-8")
        names = ["dewey_only_a_folder", "dewey_a_package", "dewey_a_module"]
        without = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        with mock.patch.object(install, "ROOT", folder), mock.patch.dict(os.environ, without, clear=True):
            self.assertEqual(install.modules_missing(names), names)   # in the current folder: not looked at
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(folder)}):
            self.assertEqual(install.modules_missing(names), ["dewey_only_a_folder"])

    def test_a_module_that_cannot_even_be_looked_up_counts_as_missing(self):
        # Looking up a dotted name imports its parent. A parent that fails to import must not turn
        # the whole answer into "could not check".
        folder = self.folder()
        (folder / "dewey_broken_parent").mkdir()
        (folder / "dewey_broken_parent" / "__init__.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(folder)}):
            self.assertEqual(install.modules_missing(["dewey_broken_parent.child", "json"]),
                             ["dewey_broken_parent.child"])

    def test_a_python_that_cannot_be_asked_gives_none(self):
        self.assertIsNone(install.modules_missing(["json"], python=str(self.folder() / "no-such-python")))
        not_a_python = self.folder() / "python"
        not_a_python.write_text("this is not a program\n", encoding="utf-8")
        self.assertIsNone(install.modules_missing(["json"], python=str(not_a_python)))

        def answers(**result):
            return mock.patch.object(install.subprocess, "run", **result)

        def finished(returncode, stdout):
            return subprocess.CompletedProcess(["python"], returncode, stdout=stdout, stderr="")

        with answers(side_effect=subprocess.TimeoutExpired(["python"], 30)):
            self.assertIsNone(install.modules_missing(["json"]))            # it hung
        with answers(return_value=finished(1, "")):
            self.assertIsNone(install.modules_missing(["json"]))            # it crashed
        with answers(return_value=finished(0, "")):
            self.assertIsNone(install.modules_missing(["json"]))            # it ran, but not the probe
        with answers(return_value=finished(0, "missing json\n")):
            self.assertIsNone(install.modules_missing(["json"]))            # it stopped halfway
        with answers(return_value=finished(0, "missing json\nchecked 1\n")):
            self.assertEqual(install.modules_missing(["json"]), ["json"])   # a full answer, for comparison

    def test_the_probe_cannot_read_typed_answers_or_print_anything(self):
        done = subprocess.CompletedProcess(["python"], 0, stderr="", stdout="\n".join([
            "a line some start-up file printed", "missing pydantic", "missing made_up_name", "missing", "checked 2"]))
        out = io.StringIO()
        with mock.patch.object(install.subprocess, "run", return_value=done) as ran, contextlib.redirect_stdout(out):
            self.assertEqual(install.modules_missing(["dotenv", "pydantic"], python="/some/python"), ["pydantic"])
        self.assertEqual(out.getvalue(), "")        # not even the "$ command" line that run() prints
        (command,), options = ran.call_args
        self.assertEqual(command[:3], ["/some/python", "-B", "-c"])
        self.assertEqual(command[4:], ["dotenv", "pydantic"])
        self.assertIs(options["stdin"], subprocess.DEVNULL)   # it can't swallow an answer typed ahead
        self.assertGreater(options["timeout"], 0)
        with mock.patch.object(install.subprocess, "run", return_value=done) as ran:
            install.modules_missing(["dotenv", "pydantic"])
        self.assertEqual(ran.call_args[0][0][0], sys.executable)   # no Python named: the wizard's own

    def test_another_python_is_asked_about_its_own_packages(self):
        # Box (d): the wizard's Python asks the Dewey folder's .venv Python, without importing
        # anything of its own.
        python = make_environment(self.folder() / ".venv", ["dewey_fake_pkg", "dewey_fake_parent.child"])
        names = ["dewey_fake_pkg", "dewey_fake_parent.child", "dewey_not_there"]
        self.assertEqual(install.modules_missing(names, python=str(python)), ["dewey_not_there"])
        self.assertEqual(install.modules_missing(names), names)


# ----------------------------------------------------------- the module lists

class ModuleListTests(unittest.TestCase):
    """Which modules stand for "requirements.txt is installed", by stack."""

    EVERY_STACK = {"dotenv", "pydantic", "mcp"}
    POSTGRES = {"psycopg", "pgvector", "numpy"}

    def listed(self):
        """Every module name the wizard can ask about, from requirements.txt and for local models."""
        setups = [CLAUDE_VOYAGE, CLAUDE_LOCAL_RERANKER, OPEN_LOCAL, AZURE,
                  {"DEWEY_STACK": "open", "EMBEDDING_PROVIDER": "voyage", "RERANK_PROVIDER": "voyage"}]
        main = {name for values in setups for name in install.requirement_modules(values)}
        local = {name for values in setups for name in install.local_model_modules(values)}
        return main, local

    def test_modules_for_each_stack(self):
        for values, expected in (
                (CLAUDE_VOYAGE, self.EVERY_STACK | self.POSTGRES | {"anthropic", "voyageai"}),
                (CLAUDE_LOCAL_RERANKER, self.EVERY_STACK | self.POSTGRES | {"anthropic", "voyageai"}),
                ({"DEWEY_STACK": "anthropic", "EMBEDDING_PROVIDER": "local", "RERANK_PROVIDER": "none"},
                 self.EVERY_STACK | self.POSTGRES | {"anthropic"}),
                (OPEN_LOCAL, self.EVERY_STACK | self.POSTGRES | {"openai"}),
                ({"DEWEY_STACK": "open", "EMBEDDING_PROVIDER": "local", "RERANK_PROVIDER": "voyage"},
                 self.EVERY_STACK | self.POSTGRES | {"openai", "voyageai"}),
                (AZURE, self.EVERY_STACK | {"openai", "azure.core", "azure.search.documents", "azure.storage.blob"})):
            with self.subTest(values=values):
                modules = install.requirement_modules(values)
                self.assertEqual(set(modules), expected)
                self.assertEqual(len(modules), len(set(modules)))

    def test_the_lists_stay_short(self):
        # Enough to tell whether requirements.txt was installed, not a copy of it.
        main, local = self.listed()
        self.assertEqual(local, set(LOCAL_MODEL_MODULES))
        self.assertLessEqual(len(main), 13)
        for left_out in ("gradio", "anyio", "dateutil", "starlette", "huggingface_hub"):
            self.assertNotIn(left_out, main | local)

    def test_local_model_packages_are_asked_for_only_where_local_models_run(self):
        # The indexer embeds and never reranks; the MCP server does both.
        for values, indexer, server in ((OPEN_LOCAL, True, True), (CLAUDE_LOCAL_RERANKER, False, True),
                                        (CLAUDE_VOYAGE, False, False), (AZURE, False, False),
                                        ({**AZURE, "EMBEDDING_PROVIDER": "local"}, False, False)):
            with self.subTest(values=values):
                self.assertEqual(install.local_model_modules(values, indexing=True),
                                 LOCAL_MODEL_MODULES if indexer else [])
                self.assertEqual(install.local_model_modules(values), LOCAL_MODEL_MODULES if server else [])

    def test_missing_packages_names_the_requirements_files_to_install(self):
        for lacks, expected in (((), []), (EVERYTHING, ["requirements.txt", "requirements-local.txt"]),
                                (["mcp"], ["requirements.txt"]), (LOCAL_MODEL_MODULES[:1], ["requirements-local.txt"]),
                                (None, None)):
            with self.subTest(lacks=lacks), probe(here=lacks):
                self.assertEqual(install.missing_packages(OPEN_LOCAL), expected)
        with probe(here=LOCAL_MODEL_MODULES):
            self.assertEqual(install.missing_packages(CLAUDE_LOCAL_RERANKER), ["requirements-local.txt"])
            self.assertEqual(install.missing_packages(CLAUDE_LOCAL_RERANKER, indexing=True), [])   # never reranks
            self.assertEqual(install.missing_packages(CLAUDE_VOYAGE), [])                          # not asked about
        with probe(here=(), venv=EVERYTHING) as asked:
            self.assertEqual(install.missing_packages(CLAUDE_VOYAGE, python="/dewey/.venv/bin/python"),
                             ["requirements.txt"])
        self.assertEqual(asked.call_count, 1)   # one fresh Python per answer
        self.assertEqual(asked.call_args[0][1], "/dewey/.venv/bin/python")

    def test_every_listed_module_is_imported_somewhere_under_app(self):
        # A package Dewey stopped using must not stay on the list: the wizard would refuse to index
        # for want of something nothing needs.
        imported = set()
        for path in (REPO / "app").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module)
        main, local = self.listed()
        for name in sorted(main | local):
            with self.subTest(module=name):
                self.assertTrue(any(used == name or used.startswith(name + ".") for used in imported),
                                f"install.py asks whether {name} is installed, but nothing under app/ imports it")

    def test_every_listed_module_is_found_where_requirements_txt_is_installed(self):
        # Guards against a package's name on PyPI (python-dotenv) written for its import name (dotenv).
        missing = requirements_txt_is_missing_here()
        if missing:
            self.skipTest(missing)
        main, _ = self.listed()
        self.assertEqual(install.modules_missing(sorted(main)), [])

    def test_the_local_model_modules_are_found_where_they_are_installed(self):
        for package in ("torch", "sentence-transformers"):
            try:
                metadata.distribution(package)
            except metadata.PackageNotFoundError:
                self.skipTest(f"the local search models' packages are not installed in this Python ({package} "
                              "is missing; CI does not install them)")
        _, local = self.listed()
        self.assertEqual(install.modules_missing(sorted(local)), [])

    def test_the_indexer_starts_with_only_the_modules_its_list_names(self):
        # What the wizard promises when it offers to index. A child Python in which every other
        # listed module is hidden goes as far as the indexer goes before it opens the database:
        # app/setup_postgres.py's imports, then the store with its embedder and reranker.
        missing = requirements_txt_is_missing_here()
        if missing:
            self.skipTest(missing)
        main, local = self.listed()
        script = "\n".join([
            "import importlib.abc, sys",
            "hidden = set(sys.argv[1].split(','))",
            "class Hide(importlib.abc.MetaPathFinder):",
            "    def find_spec(self, name, path=None, target=None):",
            "        if name in hidden or any(name.startswith(h + '.') for h in hidden):",
            "            raise ModuleNotFoundError('hidden for this test: ' + name, name=name)",
            "sys.meta_path.insert(0, Hide())",
            f"sys.path.insert(0, {str(REPO / 'app')!r})",
            "import setup_postgres",
            "from providers import build_pgvector_store, current_stack",
            "assert current_stack() != 'azure'",
            "store = build_pgvector_store()",
            "print('BUILT', type(store.embedder).__name__, type(store.reranker).__name__)",
        ])
        for values, built in ((CLAUDE_LOCAL_RERANKER, "BUILT VoyageEmbedder CrossEncoderReranker"),
                              (OPEN_LOCAL, "BUILT LocalEmbedder NoReranker")):
            with self.subTest(values=values):
                allowed = set(install.requirement_modules(values)) | set(
                    install.local_model_modules(values, indexing=True))
                # mcp and the chat SDK are on the stack's list for the server; the indexer must not need them.
                hidden = (main | local) - allowed | {"mcp", "anthropic", "openai"}
                home = tempfile.mkdtemp(prefix="dewey-packages-home-")
                self.addCleanup(shutil.rmtree, home, True)
                env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "PYTHONDONTWRITEBYTECODE": "1",
                       "VOYAGE_API_KEY": "not-a-real-key", "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/none",
                       **values}
                for needed_to_start in ("SYSTEMROOT", "LD_LIBRARY_PATH", "PYTHONPATH"):
                    if os.environ.get(needed_to_start):
                        env[needed_to_start] = os.environ[needed_to_start]
                done = subprocess.run([sys.executable, "-B", "-c", script, ",".join(sorted(hidden))], cwd=home,
                                      env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(built, done.stdout)


# ----------------------------------------------------------- the install step

class InstallStepTests(unittest.TestCase):
    """Box (b): skipping the install, or a pip error, says what that means for this Python."""

    PIP = [sys.executable, "-m", "pip", "install"]

    def install(self, values, where, answers, ran=True, here=(), verdict="ok"):
        """(printed text, questions asked, the commands the wizard ran)"""
        results = {"side_effect": list(ran)} if isinstance(ran, list) else {"return_value": ran}
        with mock.patch.object(install, "in_virtualenv", return_value=bool(where.venv)), \
                mock.patch.object(install, "python_situation", return_value=where), \
                mock.patch.object(install, "python_verdict", return_value=verdict), \
                mock.patch.object(install, "run", **results) as run, probe(here=here) as asked:
            _, out, questions = wizard(install.install_dependencies, values, answers=answers)
        self.probed = asked.call_count
        return out, questions, [call.args[0] for call in run.call_args_list]

    def test_the_two_questions_and_their_defaults_are_unchanged(self):
        out, questions, ran = self.install(CLAUDE_VOYAGE, computer("none"), ["y", ""])
        self.assertEqual(len(questions), 2)
        self.assertIn("Install into the current Python anyway? (y/N)", questions[0])
        self.assertIn("Install requirements.txt now? (Y/n)", questions[1])
        self.assertEqual(ran, [self.PIP + ["-r", "requirements.txt"]])
        _, questions, _ = self.install(CLAUDE_VOYAGE, computer("dewey"), [""])
        self.assertEqual(len(questions), 1)   # in an environment there is nothing to warn about
        self.assertIn("Install requirements.txt now? (Y/n)", questions[0])

    def test_skipping_outside_a_virtual_environment_says_what_that_means(self):
        out, questions, ran = self.install(OPEN_LOCAL, computer("none"), [""], here=EVERYTHING)
        self.assertEqual(ran, [])   # Enter means no: nothing is installed
        self.assertEqual(len(questions), 1)
        after = out.split("python install.py\n")[-1]   # what follows the hint about environments
        self.assertIn("This Python does not have Dewey's packages yet:", after)
        self.assertIn(f"    {sys.executable}\n", after)
        self.assertIn("the indexer, the MCP server and the demo page will not run with it", flat(after))
        self.assertIn(NEXT_STEPS_POINTER, after)
        self.assertNotIn("already has", out)

    def test_saying_no_to_requirements_txt_says_the_same(self):
        out, _, ran = self.install(CLAUDE_VOYAGE, computer("dewey"), ["n"], here=["anthropic", "mcp"])
        self.assertEqual(ran, [])
        self.assertIn("This Python does not have Dewey's packages yet:", out)
        self.assertIn(NEXT_STEPS_POINTER, out)

    def test_skipping_when_the_packages_are_there_says_so(self):
        for where, answers in ((computer("dewey"), ["n"]), (computer("none"), [""])):
            with self.subTest(where=where):
                out, _, ran = self.install(OPEN_LOCAL, where, answers, here=())
                self.assertEqual(ran, [])
                self.assertIn("This Python already has Dewey's packages, so nothing needs installing.", out)
                self.assertNotIn("does not have", out)
                self.assertNotIn("Next steps", out)   # nothing is left to do about packages

    def test_skipping_with_only_the_local_model_packages_missing(self):
        out, _, _ = self.install(OPEN_LOCAL, computer("dewey"), ["n"], here=LOCAL_MODEL_MODULES)
        self.assertIn("has Dewey's main packages, but not the ones for local search models", flat(out))
        self.assertIn("requirements-local.txt", out)
        self.assertIn(f"    {sys.executable}\n", out)
        self.assertIn("indexing and searches will fail", out)   # this setup embeds on this computer
        self.assertIn(NEXT_STEPS_POINTER, out)
        self.assertNotIn("does not have Dewey's packages yet", out)
        # With hosted embeddings and a local reranker, indexing still works.
        out, _, _ = self.install(CLAUDE_LOCAL_RERANKER, computer("dewey"), ["n"], here=LOCAL_MODEL_MODULES)
        self.assertIn("searches will fail", out)
        self.assertNotIn("indexing and searches", out)
        # A setup that runs no local models is not asked about them at all.
        out, _, _ = self.install(CLAUDE_VOYAGE, computer("dewey"), ["n"], here=LOCAL_MODEL_MODULES)
        self.assertIn("already has Dewey's packages", out)

    def test_a_check_that_could_not_be_made_says_so(self):
        out, _, ran = self.install(OPEN_LOCAL, computer("dewey"), ["n"], here=None)
        self.assertEqual(ran, [])
        self.assertIn("This wizard could not check whether this Python has Dewey's packages:", out)
        self.assertIn(f"    {sys.executable}\n", out)
        for claim in ("does not have", "already has", "Next steps"):
            self.assertNotIn(claim, out)

    def test_a_pip_error_says_what_it_means_and_how_to_try_again(self):
        out, _, ran = self.install(CLAUDE_VOYAGE, computer("dewey"), [""], ran=False, here=EVERYTHING)
        self.assertEqual(ran, [self.PIP + ["-r", "requirements.txt"]])
        self.assertIn("pip stopped with an error", out)
        self.assertIn("This Python does not have Dewey's packages yet:", out)
        self.assertIn("may be only partly installed", out)
        self.assertIn("To try again, run this wizard again.", out)
        self.assertIn(NEXT_STEPS_POINTER, out)
        self.assertNotIn("pip failed; see the output above.", out)   # all the old message said
        self.assertNotIn("is the safest choice", out)                 # this Python is one Dewey is tested with
        assert_commands_stand_alone(self, out)

    def test_a_pip_error_never_ends_with_this_python_already_has_them(self):
        # pip just failed. Whatever the short list of modules says, that is not "nothing to do".
        out, _, _ = self.install(CLAUDE_VOYAGE, computer("dewey"), [""], ran=False, here=())
        self.assertIn("pip stopped with an error", out)
        self.assertIn("may be only partly installed", out)
        self.assertNotIn("already has", out)
        self.assertNotIn("Next steps", out)   # which would list no package step

    def test_a_pip_error_on_a_python_newer_than_the_tested_ones_points_to_the_tested_one(self):
        with mock.patch.object(install, "TESTED_PYTHON", (3, 13)):
            out, _, _ = self.install(CLAUDE_VOYAGE, computer("dewey"), [""], ran=False, here=EVERYTHING,
                                     verdict="untested")
        (line,) = [line for line in out.splitlines() if "is the safest choice" in line]   # one line
        self.assertIn("not tested with Python %d.%d" % sys.version_info[:2], line)
        self.assertIn("Python 3.13 is the safest choice", line)                          # from the constant
        self.assertIn("Troubleshooting in docs/INSTALL.md", line)
        # Skipping an install is not a pip error: no such line then.
        out, _, _ = self.install(CLAUDE_VOYAGE, computer("dewey"), ["n"], here=EVERYTHING, verdict="untested")
        self.assertNotIn("not tested with", out)

    def test_a_failed_pytorch_install_stops_before_requirements_local(self):
        # Going on would fetch the full PyTorch build, against the answer just given.
        out, questions, ran = self.install(OPEN_LOCAL, computer("dewey"), ["", ""], ran=[True, False],
                                           here=LOCAL_MODEL_MODULES)
        self.assertEqual(ran, [self.PIP + ["-r", "requirements.txt"],
                               self.PIP + ["torch", "--index-url", install.LINKS["pytorch_cpu"]]])
        self.assertIn("lack an NVIDIA GPU", questions[1])
        self.assertIn("pip stopped with an error", out)
        self.assertIn("not the ones for local search models", out)
        self.assertIn("To try again, run this wizard again.", out)
        self.assertIn(NEXT_STEPS_POINTER, out)
        assert_commands_stand_alone(self, out)

    def test_a_failed_requirements_local_install_is_reported(self):
        for answers, calls in ((["", ""], 3), (["", "n"], 2)):   # with and without the CPU-only PyTorch first
            with self.subTest(answers=answers):
                out, _, ran = self.install(OPEN_LOCAL, computer("dewey"), answers, ran=[True] * (calls - 1) + [False],
                                           here=LOCAL_MODEL_MODULES)
                self.assertEqual(len(ran), calls)
                self.assertEqual(ran[-1], self.PIP + ["-r", "requirements-local.txt"])
                self.assertIn("pip stopped with an error", out)
                self.assertIn("not the ones for local search models", out)

    def test_a_clean_install_says_nothing_more_and_asks_no_python_anything(self):
        for values, answers, calls in ((CLAUDE_VOYAGE, [""], 1), (OPEN_LOCAL, ["", ""], 3)):
            with self.subTest(values=values):
                out, _, ran = self.install(values, computer("dewey"), answers, here=EVERYTHING)
                self.assertEqual(len(ran), calls)
                self.assertEqual(self.probed, 0)
                for worry in ("does not have", "pip stopped", "could not check", "Next steps"):
                    self.assertNotIn(worry, out)


# ------------------------------------------------------------- the index step

class IndexStepTests(TempRootTestCase):
    """Boxes (b) and (c): the wizard starts the indexer only with a Python that can run it, only
    when asked to, and says how the run ended."""

    def index(self, values, answers=(), ran=True, here=()):
        """(what index_archive() returned, printed text, questions asked, the commands it ran)"""
        results = {"side_effect": ran} if isinstance(ran, BaseException) else {"return_value": ran}
        with mock.patch.object(install, "run", **results) as run, probe(here=here) as asked:
            try:
                result, out, questions = wizard(install.index_archive, values, answers=answers)
            except KeyboardInterrupt:   # left alone, it would end the whole test run
                self.fail("Control+C while the indexer ran ended the wizard, so nothing after the index step "
                          "would be shown: not \"Next steps\", not the command to index again")
        self.probed = asked.call_count
        return result, out, questions, [call.args[0] for call in run.call_args_list]

    def test_the_indexer_is_not_started_with_a_python_that_lacks_deweys_packages(self):
        # The issue's first test. It used to be started, and to stop at its first import.
        self.articles()
        asked, out = [], io.StringIO()

        def yes(prompt=""):
            asked.append(prompt)
            return "y"

        with probe(here=EVERYTHING), mock.patch.object(install, "run", return_value=True) as run, \
                mock.patch.object(install, "_STYLED", False), contextlib.redirect_stdout(out), \
                mock.patch("builtins.input", yes):
            result = install.index_archive(OPEN_LOCAL)
        run.assert_not_called()
        self.assertEqual(result, "no-packages")
        self.assertEqual(asked, [], "there is nothing to ask when the indexer can't run")
        said = out.getvalue()
        self.assertIn("Found 1 JSON file(s) in data/.", said)
        self.assertIn("The indexer cannot run with this Python, because it does not have Dewey's packages:", said)
        self.assertIn(f"  {sys.executable}\n", said)
        self.assertIn("has not started it", flat(said))
        self.assertIn('"Next steps", below, says what to do', flat(said))

    def test_the_same_on_azure_where_the_indexer_is_app_setup_py(self):
        self.articles(2)
        result, out, questions, ran = self.index(AZURE, here=["azure.storage.blob"])
        self.assertEqual((result, questions, ran), ("no-packages", [], []))
        self.assertIn("cannot run with this Python", out)

    def test_local_model_packages_hold_up_indexing_only_when_embeddings_are_local(self):
        self.articles()
        result, out, questions, ran = self.index(OPEN_LOCAL, here=LOCAL_MODEL_MODULES)
        self.assertEqual((result, questions, ran), ("no-packages", [], []))
        self.assertIn("it lacks the packages for local search models (PyTorch and requirements-local.txt)", flat(out))
        self.assertNotIn("does not have Dewey's packages", out)   # it has the main ones
        # Hosted embeddings with a local reranker: the indexer never reranks, so it can run.
        result, out, questions, ran = self.index(CLAUDE_LOCAL_RERANKER, answers=[""], here=LOCAL_MODEL_MODULES)
        self.assertEqual((result, ran), ("declined", []))
        self.assertEqual(len(questions), 1)

    def test_enter_does_not_index_and_the_line_above_the_question_says_why(self):
        self.articles(3)
        for values, reason in ((CLAUDE_VOYAGE, r"Voyage AI, which bills per token"),
                               (OPEN_LOCAL, r"on this computer, which can take a long time"),
                               ({**OPEN_LOCAL, "RERANK_PROVIDER": "voyage"}, r"on this computer"),
                               (AZURE, r"Azure, which embeds each one and bills for it")):
            with self.subTest(values=values):
                result, out, questions, ran = self.index(values, answers=[""])
                self.assertEqual((result, ran), ("declined", []))
                self.assertEqual(len(questions), 1)
                self.assertEqual(questions[0], "Index them now? (y/N): ")   # the prompt the guides quote
                lines = out.rstrip("\n").split("\n")
                self.assertEqual(lines[-3], "Found 3 JSON file(s) in data/.")
                self.assertRegex(lines[-2], reason)                          # one sentence, for this setup
                self.assertTrue(lines[-2].endswith(", so it starts only if you type y."), lines[-2])
                self.assertEqual(lines[-2].count(". "), 0, "one sentence")
                self.assertIn("Not indexed now.", lines[-1])
                self.assertIn('"Next steps"', lines[-1])
                self.assertNotIn("could not check", out)

    def test_yes_starts_the_indexer_with_this_python(self):
        self.articles()
        result, out, _, ran = self.index(OPEN_LOCAL, answers=["y"])
        self.assertEqual(result, "indexed")
        self.assertEqual(ran, [[sys.executable, "app/setup_postgres.py"]])
        self.assertNotIn("Error or Failed", out)   # the Postgres indexer's exit status can be trusted
        self.assertEqual(self.probed, 1)           # asked once, about the indexer's own needs

    def test_azure_runs_app_setup_py_and_says_to_read_its_output(self):
        self.articles()
        result, out, _, ran = self.index(AZURE, answers=["y"])
        self.assertEqual(result, "indexed")
        self.assertEqual(ran, [[sys.executable, "app/setup.py"]])
        after = out.split("starts only if you type y.")[1]
        self.assertIn("app/setup.py has finished", after)
        self.assertIn("Error or Failed", flat(after))   # it exits 0 after upload and indexer errors too
        self.assertIn("docs/AZURE.md", after)

    def test_an_indexer_that_exits_with_an_error_is_reported(self):
        self.articles()
        for values, script in ((OPEN_LOCAL, "app/setup_postgres.py"), (AZURE, "app/setup.py")):
            with self.subTest(values=values):
                result, out, _, ran = self.index(values, answers=["y"], ran=False)
                self.assertEqual(result, "failed")
                self.assertEqual(ran, [[sys.executable, script]])
                self.assertIn("The indexer stopped with an error", out)
                self.assertIn('"Next steps"', out)
                self.assertNotIn("has finished", out)

    def test_control_c_during_indexing_is_reported_and_the_wizard_carries_on(self):
        self.articles()
        result, out, _, ran = self.index(OPEN_LOCAL, answers=["y"], ran=KeyboardInterrupt())
        self.assertEqual(result, "stopped")   # returned, not raised: "Next steps" still follows
        self.assertEqual(len(ran), 1)
        self.assertIn("Indexing was stopped before it finished.", out)
        # Each article is saved as soon as it is embedded, and a second run embeds them all again
        # (PgVectorStore.index_documents).
        self.assertIn("Articles indexed so far are kept", flat(out))
        self.assertIn("embeds every article again", flat(out))
        result, out, _, _ = self.index(AZURE, answers=["y"], ran=KeyboardInterrupt())
        self.assertEqual(result, "stopped")
        self.assertIn("was stopped before it finished", out)
        self.assertNotIn("embeds every article again", flat(out))   # on azure, Azure embeds, later
        self.assertIn("uploads every article again", flat(out))

    def test_control_c_at_the_question_still_cancels_the_wizard(self):
        self.articles()
        with probe(), mock.patch.object(install, "run") as run, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                install.index_archive(OPEN_LOCAL)
        run.assert_not_called()

    def test_no_data_files_asks_nothing_and_no_python_anything(self):
        for values in (OPEN_LOCAL, AZURE):
            with self.subTest(values=values):
                result, out, questions, ran = self.index(values, here=EVERYTHING)
                self.assertEqual((result, questions, ran), ("no-data", [], []))
                self.assertEqual(self.probed, 0)
                self.assertIn("No .json files in data/ yet, so there is nothing to index.", out)

    def test_a_check_that_could_not_be_made_says_so_in_one_line_and_still_asks(self):
        self.articles()
        result, out, questions, ran = self.index(OPEN_LOCAL, answers=["y"], here=None)
        self.assertEqual(result, "indexed")
        self.assertEqual(ran, [[sys.executable, "app/setup_postgres.py"]])
        (line,) = [line for line in out.splitlines() if "could not check" in line]
        self.assertIn("Dewey's packages", line)
        self.assertLess(out.index("could not check"), out.index("starts only if you type y."))
        self.assertNotIn("cannot run", out)


# ----------------------------------------------------------------- Next steps

class NextStepsTests(TempRootTestCase):
    """Box (c): a numbered list of what is left, in the order to do it."""

    def steps(self, values, missing=(), indexing="declined", where=None, here=(), venv=EVERYTHING):
        with probe(here=here, venv=venv) as asked:
            steps = install.remaining_steps(values, list(missing), indexing, where or computer("dewey"))
        self.asked = asked
        return steps

    def test_indexing_is_listed_whenever_it_did_not_finish_in_this_run(self):
        self.articles()
        for indexing, title in (("declined", r"unless it is already indexed"),
                                ("failed", r"once the error above is fixed"),
                                ("stopped", r"again"), ("no-packages", r"^Index your archive:$"),
                                ("no-data", r"^Index your archive:$"), ("no-settings", r"^Index your archive:$")):
            with self.subTest(indexing=indexing):
                steps = self.steps(OPEN_LOCAL, indexing=indexing)
                self.assertEqual(len(steps), 1)
                self.assertRegex(steps[0][0], title)
                self.assertTrue(steps[0][0].endswith(":"))
                self.assertEqual(steps[0][1:], ["  python app/setup_postgres.py"])   # on a line of its own
        self.assertEqual(self.steps(OPEN_LOCAL, indexing="indexed"), [])   # nothing left: no numbered lines

    def test_azure_names_app_setup_py_even_with_settings_missing(self):
        self.articles()
        steps = self.steps(AZURE, missing=["AZURE_SEARCH_API_KEY", "AZURE_OPENAI_ENDPOINT"], indexing="no-settings")
        self.assertEqual(len(steps), 2)
        self.assertIn("AZURE_SEARCH_API_KEY, AZURE_OPENAI_ENDPOINT", steps[0][0])
        self.assertEqual(steps[0][-1], "  python install.py")
        self.assertEqual(steps[1], ["Index your archive:", "  python app/setup.py"])
        for indexing in ("declined", "failed", "stopped", "no-packages", "no-data"):
            self.assertEqual(self.steps(AZURE, indexing=indexing)[-1][-1], "  python app/setup.py")

    def test_steps_come_in_the_order_they_must_be_done(self):
        # Nothing done yet: settings missing, no packages, no articles.
        steps = self.steps(CLAUDE_VOYAGE, missing=["DATABASE_URL"], indexing="no-settings", here=EVERYTHING)
        self.assertEqual(len(steps), 4)
        self.assertIn("DATABASE_URL", steps[0][0])
        self.assertIn("  python -m pip install -r requirements.txt", steps[1])
        self.assertIn("data", steps[2][0])
        self.assertIn("docs/IMPORTING.md", " ".join(steps[2]))
        self.assertEqual(steps[2][-1], "  python app/import_archive.py --help")
        self.assertEqual(steps[3][-1], "  python app/setup_postgres.py")
        self.articles()
        steps = self.steps(CLAUDE_VOYAGE, indexing="no-packages", here=EVERYTHING)
        self.assertEqual([step[-1] for step in steps],
                         ["  python -m pip install -r requirements.txt", "  python app/setup_postgres.py"])

    def test_inside_a_virtual_environment_the_packages_are_installed_with_pip(self):
        self.articles()
        for where in (computer("dewey"), computer("other")):
            with self.subTest(where=where):
                (step, _) = self.steps(CLAUDE_VOYAGE, where=where, here=EVERYTHING)
                self.assertEqual(commands(step), ["python -m pip install -r requirements.txt"])
                self.assertIn("virtual environment turned on", words(step))   # `python` is its Python only then
        # For local search models, the wizard is run again: it asks which PyTorch fits this computer.
        (step, _) = self.steps(OPEN_LOCAL, where=computer("dewey"), here=EVERYTHING)
        self.assertEqual(commands(step), ["python -m pip install -r requirements.txt", "python install.py"])
        self.assertIn("local search models", " ".join(step))
        (step, _) = self.steps(OPEN_LOCAL, where=computer("dewey"), here=LOCAL_MODEL_MODULES)
        self.assertEqual(commands(step), ["python install.py"])
        self.assertIn("local search models", words(step))
        self.assertIn("requirements-local.txt", words(step))

    def test_the_pip_commands_start_in_the_dewey_folder_when_the_terminal_is_somewhere_else(self):
        # `-r requirements.txt` and `python install.py` are relative. The other forms of this step
        # begin with `cd`; this one used to leave it out.
        self.articles()
        pip, again = "python -m pip install -r requirements.txt", "python install.py"
        for where, cd in ((computer("dewey", "linux", in_root=False), "cd /home/you/dewey-ai"),
                          (computer("other", "mac", in_root=False), "cd /Users/you/dewey-ai"),
                          (computer("dewey", "windows", in_root=False), "cd C:\\Users\\you\\dewey-ai")):
            for values, here, expected in ((CLAUDE_VOYAGE, EVERYTHING, [cd, pip]),
                                           (OPEN_LOCAL, EVERYTHING, [cd, pip, again]),
                                           (OPEN_LOCAL, LOCAL_MODEL_MODULES, [cd, again])):
                with self.subTest(where=where, values=values, here=here):
                    (step, _) = self.steps(values, where=where, here=here)
                    self.assertEqual(commands(step), expected)
                    self.assertIn("go to the Dewey folder", words(step))
                    self.assertFalse(step[0].startswith("  "))
                    for at, line in enumerate(step):
                        if line.startswith("  "):   # each command under a line that says what it does
                            self.assertTrue(step[at - 1].endswith(":") and not step[at - 1].startswith("  "), step)
        # In the Dewey folder there is nowhere to go: the step is as short as before.
        (step, _) = self.steps(CLAUDE_VOYAGE, where=computer("dewey"), here=EVERYTHING)
        self.assertEqual(step, ["Install the packages Dewey needs, with the virtual environment turned on:",
                                "  " + pip])

    def test_the_wizard_is_run_again_once_and_only_after_its_environment_is_on(self):
        # Settings and packages both missing, outside the environment the packages belong in:
        # step 1 used to say `python install.py`, which there is often no command at all (on a
        # Mac), or is the Python without the packages, and step 2 ended with it again.
        self.articles()
        missing = ["DATABASE_URL", "VOYAGE_API_KEY"]
        for where in (computer("none"), computer("off"), computer("broken"), computer("other+own"),
                      computer("none", "linux", in_root=False), computer("off", "windows")):
            for venv in (EVERYTHING, ()):   # the .venv lacks the packages too, or already has them
                with self.subTest(where=where, venv=venv):
                    steps = self.steps(CLAUDE_VOYAGE, missing=missing, indexing="no-settings", where=where,
                                       here=EVERYTHING, venv=venv)
                    settings, packages = steps[0], steps[1]
                    self.assertEqual(commands(settings), [])
                    self.assertIn("DATABASE_URL, VOYAGE_API_KEY", settings[0])
                    self.assertIn("Edit .env, or type what is missing when you run this wizard again in step 2.",
                                  settings[0])
                    printed = [line.strip() for step in steps for line in step]
                    self.assertEqual(printed.count("python install.py"), 1)
                    (turn_on,) = [at for at, line in enumerate(printed) if "activate" in line.lower()]
                    self.assertLess(turn_on, printed.index("python install.py"))
                    self.assertEqual(packages[-1], "  python install.py")
                    self.assertEqual("Nothing needs installing" in words(packages),
                                     venv == () and where.dewey_venv_exists)
        # With every setting filled in, a .venv that has the packages still only has to be turned on.
        (step, _) = self.steps(CLAUDE_VOYAGE, where=computer("off"), here=EVERYTHING, venv=())
        self.assertEqual(commands(step), ["source .venv/bin/activate"])
        # Inside the environment, `python` is its Python: the settings step keeps its command.
        for where in (computer("dewey"), computer("other")):
            with self.subTest(where=where):
                steps = self.steps(CLAUDE_VOYAGE, missing=missing, indexing="no-settings", where=where, here=EVERYTHING)
                self.assertEqual(steps[0][-1], "  python install.py")
                self.assertIn("Edit .env, or run this wizard again:", steps[0][0])
                self.assertEqual(commands(steps[1]), ["python -m pip install -r requirements.txt"])
        # And so it does when no package is missing, or the check could not be made.
        for here in ((), None):
            with self.subTest(here=here):
                steps = self.steps(CLAUDE_VOYAGE, missing=missing, indexing="no-settings", where=computer("none"),
                                   here=here)
                self.assertEqual(steps[0][-1], "  python install.py")
                self.assertEqual(len(steps), 2)   # settings, then indexing

    def test_outside_one_the_steps_make_or_turn_on_the_environment_and_run_the_wizard_again(self):
        # "Outside one" includes another project's environment that was left turned on, when the
        # Dewey folder has its own: Dewey's packages do not belong in the other project's.
        self.articles()
        for where in (computer("none"), computer("off"), computer("broken"), computer("none", "linux", in_root=False),
                      computer("other+own"), computer("off", "windows")):
            with self.subTest(where=where):
                (step, _) = self.steps(OPEN_LOCAL, where=where, here=EVERYTHING)
                shared = install.environment_steps(where)
                self.assertEqual(step[-len(shared):], shared)       # the same lines as the install step's hint
                self.assertEqual(step[-1], "  python install.py")
                self.assertNotIn("pip install", "\n".join(step[:-len(shared)]))
                self.assertIn("press Enter to keep each answer", words(step))
        self.assertEqual(self.asked.call_count, 2)   # this Python, then the .venv that is there

    def test_a_dot_venv_that_already_has_the_packages_only_has_to_be_turned_on(self):
        self.articles()
        for where, activate in ((computer("off"), "source .venv/bin/activate"),
                                (computer("other+own"), "source .venv/bin/activate"),
                                (computer("off", "windows"), ".venv\\Scripts\\Activate.ps1")):
            for here in (EVERYTHING, LOCAL_MODEL_MODULES):
                with self.subTest(where=where, here=here):
                    (step, index) = self.steps(OPEN_LOCAL, where=where, here=here, venv=())
                    self.assertEqual(commands(step), [activate])
                    self.assertIn("Nothing needs installing", words(step))
                    for never in ("python install.py", "-m venv", "pip install"):
                        self.assertNotIn(never, "\n".join(step))
                    self.assertEqual(index[-1], "  python app/setup_postgres.py")
                    # The .venv's own Python was the one asked, and about everything this setup runs.
                    python = self.asked.call_args_list[-1][0][1]
                    self.assertEqual(python, str(install.venv_python()))
        away = computer("off", "linux", in_root=False)
        (step, _) = self.steps(OPEN_LOCAL, where=away, here=EVERYTHING, venv=())
        self.assertEqual(commands(step), ["cd /home/you/dewey-ai", "source .venv/bin/activate"])

    def test_a_dot_venv_is_only_believed_when_it_could_be_asked_and_has_everything(self):
        self.articles()
        for venv in (None, LOCAL_MODEL_MODULES, ["mcp"]):
            with self.subTest(venv=venv):
                (step, _) = self.steps(OPEN_LOCAL, where=computer("off"), here=EVERYTHING, venv=venv)
                self.assertEqual(commands(step), ["source .venv/bin/activate", "python install.py"])
                self.assertNotIn("Nothing needs installing", words(step))
        # The wizard's own environment is not asked twice, and a folder that is not a working
        # environment is not asked at all.
        for where in (computer("dewey"), computer("broken"), computer("none"), computer("other")):
            with self.subTest(where=where):
                self.steps(OPEN_LOCAL, where=where, here=EVERYTHING, venv=())
                self.assertEqual(self.asked.call_count, 1)

    def test_no_package_step_when_nothing_is_missing_or_the_check_could_not_be_made(self):
        self.articles()
        for here in ((), None):
            for where in (computer("none"), computer("off"), computer("dewey")):
                with self.subTest(here=here, where=where):
                    steps = self.steps(OPEN_LOCAL, where=where, here=here, venv=())
                    self.assertEqual([step[-1] for step in steps], ["  python app/setup_postgres.py"])
                    self.assertEqual(self.asked.call_count, 1)

    def situations(self):
        for system in ("mac", "linux"):
            for lives in ("none", "off", "broken", "dewey", "other", "other+own"):
                for in_root in (True, False):
                    yield computer(lives, system, in_root)

    def test_printed_commands_paste_safely_into_zsh(self):
        # These lines are built, not written out in say(), so tests/docs does not see them.
        checked = 0
        for where in self.situations():
            for values in (CLAUDE_VOYAGE, OPEN_LOCAL, AZURE):
                for here, venv in ((EVERYTHING, EVERYTHING), (EVERYTHING, ()), (LOCAL_MODEL_MODULES, ()), ((), ())):
                    for indexing in ("no-settings", "declined", "failed", "stopped"):
                        steps = self.steps(values, missing=["DATABASE_URL"], indexing=indexing, where=where,
                                           here=here, venv=venv)
                        for command in commands(line for step in steps for line in step):
                            with self.subTest(where=where, command=command):
                                self.assertEqual(shell_problems(command), [])
                                self.assertNotRegex(command, r"[,.:]$")
                                checked += 1
        self.assertGreater(checked, 500)

    def test_the_words_between_the_commands_do_no_harm_if_they_are_pasted_too(self):
        # A person may select a whole step, or all of them. A numbered line starts with "1.", which
        # is not a command; the rest have no quotes, apostrophes, brackets or other characters a
        # shell acts on, and no line starts with a word that is also a command.
        for where in self.situations():
            for values in (OPEN_LOCAL, AZURE):
                for here, venv in ((EVERYTHING, EVERYTHING), (EVERYTHING, ()), (LOCAL_MODEL_MODULES, EVERYTHING)):
                    for indexing in ("no-settings", "declined", "failed", "stopped"):
                        steps = self.steps(values, missing=["DATABASE_URL", "VOYAGE_API_KEY"], indexing=indexing,
                                           where=where, here=here, venv=venv)
                        for step in steps:
                            for line in step:
                                if line.startswith("  "):
                                    continue
                                with self.subTest(where=where, line=line):
                                    self.assertEqual(DANGEROUS & set(line), set())
                                    if line is not step[0]:   # the first line is printed after its number
                                        self.assertNotIn(line.split()[0].lower().rstrip(":,."), ALSO_COMMANDS)

    def test_say_steps_numbers_the_steps_and_sets_the_commands_apart(self):
        steps = [["Fill in what is missing:", "  python install.py"],
                 ["Index your archive:", "Words about it:", "  python app/setup_postgres.py"]]
        _, out, _ = wizard(install.say_steps, steps)
        self.assertEqual(out, "1. Fill in what is missing:\n"
                              "     python install.py\n"
                              "2. Index your archive:\n"
                              "   Words about it:\n"
                              "     python app/setup_postgres.py\n")
        self.assertEqual(wizard(install.say_steps, [])[1], "")


# --------------------------------------------------- the Python the MCP example names

class McpPythonTests(TempRootTestCase):
    """Box (d): the example names a Python that has Dewey's packages, or says what to change."""

    def snippet(self, values, where, here=(), venv=EVERYTHING):
        """((the Python named, how it was chosen), the printed example)"""
        with probe(here=here, venv=venv) as asked:
            chosen = install.mcp_python(values, where)
            _, out, _ = wizard(install.print_mcp_next_steps, values, where)
        self.asked = asked
        return chosen, out

    def assert_names(self, out, python):
        server = self.root / "app" / "mcp_server.py"
        self.assertIn(f'        command: "{python}"\n', out)
        self.assertIn(f'        args: ["{server}"]\n', out)
        self.assertIn("    " + install.shell_join([str(python), str(server), "--http"]) + "\n", out)
        self.assertIn("    mcp_servers:\n", out)

    def test_this_python_is_named_when_it_has_the_packages(self):
        for where in (computer("dewey"), computer("none"), computer("off"), computer("other")):
            with self.subTest(where=where):
                chosen, out = self.snippet(OPEN_LOCAL, where, here=(), venv=())
                self.assertEqual(chosen, (sys.executable, "this"))
                self.assert_names(out, sys.executable)
                for note in ("change command:", "could not check", "will not work", "virtual environment"):
                    self.assertNotIn(note, out)
                self.assertEqual(self.asked.call_count, 2)   # once to choose, once to print: no other Python

    def test_the_dewey_folders_venv_is_named_when_this_python_lacks_the_packages(self):
        for where in (computer("off"), computer("other+own")):
            with self.subTest(where=where):
                chosen, out = self.snippet(CLAUDE_VOYAGE, where, here=EVERYTHING, venv=())
                self.assertEqual(chosen, (str(install.venv_python()), "venv"))
                self.assert_names(out, install.venv_python())
                self.assertNotIn(sys.executable, out)
                note = flat(out.split("mcp_servers:")[0])
                self.assertIn("virtual environment in the Dewey folder", note)
                self.assertIn("has the packages Dewey needs", note)
                self.assertIn("the Python running this wizard does not have them all", note)
                self.assertNotIn("change command:", out)

    def test_it_is_also_named_when_this_python_lacks_only_the_local_model_packages(self):
        # Named without a word, the wizard's Python would start the server and fail at the first search.
        chosen, out = self.snippet(OPEN_LOCAL, computer("off"), here=LOCAL_MODEL_MODULES, venv=())
        self.assertEqual(chosen, (str(install.venv_python()), "venv"))
        self.assert_names(out, install.venv_python())
        self.assertIn("the Python running this wizard does not have them all", flat(out.split("mcp_servers:")[0]))

    def test_no_python_with_the_packages_says_so_and_to_change_command(self):
        # Outside an environment the steps above put the packages in the Dewey folder's .venv;
        # inside one, in the Python running the wizard.
        dewey_folder, this_one = "virtual environment in the Dewey folder", "the Python running this wizard"
        for where, python, named in ((computer("none"), install.venv_python, dewey_folder),
                                     (computer("off"), install.venv_python, dewey_folder),
                                     (computer("broken"), install.venv_python, dewey_folder),
                                     (computer("other+own"), install.venv_python, dewey_folder),
                                     (computer("dewey"), lambda: sys.executable, this_one),
                                     (computer("other"), lambda: sys.executable, this_one)):
            for here in (EVERYTHING, LOCAL_MODEL_MODULES):
                with self.subTest(where=where, here=here):
                    chosen, out = self.snippet(OPEN_LOCAL, where, here=here, venv=EVERYTHING)
                    self.assertEqual(chosen, (str(python()), "none"))
                    self.assert_names(out, python())
                    note = flat(out.split("mcp_servers:")[0])
                    self.assertIn("will not work until the packages Dewey needs are installed", note)
                    self.assertIn("the steps above", note)
                    self.assertIn(named, note)
                    self.assertIn("change command: and the HTTP command", note)

    def test_a_check_that_could_not_be_made_names_this_python_and_says_so(self):
        for where in (computer("none"), computer("off"), computer("dewey")):
            with self.subTest(where=where):
                chosen, out = self.snippet(OPEN_LOCAL, where, here=None, venv=())
                self.assertEqual(chosen, (sys.executable, "unknown"))
                self.assert_names(out, sys.executable)
                note = flat(out.split("mcp_servers:")[0])
                self.assertIn("could not check that this Python has the packages Dewey needs", note)
                self.assertIn("If your AI tool says the server failed to start", note)
                self.assertIn("change command: and the HTTP command to the Python that has them", note)
                self.assertNotIn("will not work until", out)

    def test_a_dot_venv_is_named_only_when_it_could_be_asked_and_has_everything(self):
        for venv in (None, ["mcp"], LOCAL_MODEL_MODULES):
            with self.subTest(venv=venv):
                chosen, _ = self.snippet(OPEN_LOCAL, computer("off"), here=EVERYTHING, venv=venv)
                self.assertEqual(chosen, (str(install.venv_python()), "none"))   # where the steps will put them

    def test_the_wizards_own_environment_is_not_asked_twice(self):
        chosen, _ = self.snippet(OPEN_LOCAL, computer("dewey"), here=EVERYTHING, venv=())
        self.assertEqual(chosen, (sys.executable, "none"))
        self.assertTrue(all(len(call[0]) == 1 or call[0][1] is None for call in self.asked.call_args_list))

    def test_the_rest_of_the_example_is_as_before(self):
        _, out = self.snippet(OPEN_LOCAL, computer("dewey"))
        lines = out.splitlines()
        self.assertEqual(lines[0], "Connect an AI tool (full recipes for each client: docs/MCP.md):")
        self.assertEqual(lines[1], "  On this computer (stdio), e.g. Hermes Agent's ~/.hermes/config.yaml:")
        self.assertIn("          include: [search_archive]", lines)
        self.assertIn("  Over HTTP (shared server). This listens on 127.0.0.1:8765:", lines)
        self.assertIn("Authorization: Bearer <DEWEY_MCP_TOKEN from .env>", out)
        self.assertEqual(lines[-1], "  Put TLS in front before exposing it beyond this machine (docs/DEPLOYMENT.md).")


# ------------------------------------------------------- main(), from the install step on

class EndOfWizardTests(TempRootTestCase):
    """main() from "Write this to .env?" to its last line, with the earlier screens answered."""

    SCREENS = {
        "ask_open_llm": {"OPEN_LLM_BASE_URL": "", "OPEN_LLM_MODEL": "qwen3:8b"},
        "ask_retrieval": {"EMBEDDING_PROVIDER": "local", "RERANK_PROVIDER": "none"},
        "ask_postgres": {"DATABASE_URL": "postgresql://dewey:not-a-real-password@127.0.0.1:5432/dewey"},
        "ask_newsroom": {"NEWSROOM_NAME": "The Example Times"},
        "ask_mcp": {"DEWEY_MCP_TOKEN": "t" * 43},
    }

    def main(self, answers, *args, screens=None, where=None, here=(), venv=EVERYTHING, **replaced):
        """(exit status, printed text, the mocks). `replaced` maps a wizard function's name to what
        it returns instead of running."""
        screens = {**self.SCREENS, **(screens or {})}
        replaced = {"install_dependencies": None, "get_archive": None, **replaced}
        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(mock.patch.object(sys, "argv", ["install.py", "--no-browser", *args]))
            enter(mock.patch.object(install, "check_python", return_value=True))
            enter(mock.patch.object(install, "ask_stack", return_value="open"))
            enter(mock.patch.object(install, "python_situation", return_value=where or computer("dewey")))
            for name, values in screens.items():
                enter(mock.patch.object(install, name, return_value=values))
            mocks = {name: enter(mock.patch.object(install, name, return_value=value))
                     for name, value in replaced.items()}
            mocks["run"] = enter(mock.patch.object(install, "run", return_value=True))
            mocks["subprocess.run"] = enter(mock.patch.object(install.subprocess, "run", side_effect=AssertionError(
                "the wizard started a program")))
            mocks["probe"] = enter(probe(here=here, venv=venv))
            status, out, _ = wizard(install.main, answers=answers)
        return status, out, mocks

    def next_steps(self, out):
        """What is printed under the "Next steps" heading (earlier screens name it too)."""
        return out.split("\u2500\u2500 Next steps ", 1)[1].split("\n", 1)[1]

    def test_dry_run_asks_no_python_anything(self):
        # --dry-run promises to run nothing: it stops before the install step, the index step and
        # "Next steps", so no Python is asked about its packages.
        self.articles()
        status, out, mocks = self.main([], "--dry-run", here=EVERYTHING)
        self.assertEqual(status, 0)
        self.assertIn("(--dry-run: nothing was written.)", out)
        self.assertNotIn("Next steps", out)
        self.assertNotIn("mcp_servers:", out)
        mocks["probe"].assert_not_called()
        mocks["run"].assert_not_called()
        mocks["install_dependencies"].assert_not_called()
        mocks["get_archive"].assert_called_once_with(dry_run=True)
        self.assertFalse((self.root / ".env").exists())

    def test_enter_at_index_them_now_leaves_the_command_in_next_steps(self):
        self.articles()
        # Enter writes .env; n skips the service check (it would reach for Ollama and Postgres);
        # Enter at "Index them now?" means no.
        status, out, mocks = self.main(["", "n", ""])
        self.assertEqual(status, 0)
        mocks["run"].assert_not_called()
        self.assertTrue(self.next_steps(out).startswith(
            "1. Index your archive, unless it is already indexed and the files in data/ have not changed:\n"
            "     python app/setup_postgres.py\n"
            "Connect an AI tool (full recipes for each client: docs/MCP.md):\n"), self.next_steps(out))
        self.assertIn(f'command: "{sys.executable}"', out)
        self.assertTrue(out.rstrip().endswith("each citation against the article it links to."))   # as before
        self.assertIn("Optional demo page (this computer only):  python app/main.py", out)
        self.assertIn("DEWEY_STACK='open'", (self.root / ".env").read_text(encoding="utf-8"))

    def test_nothing_left_to_do_prints_no_numbered_lines(self):
        self.articles()
        status, out, mocks = self.main(["", "n", "y"])
        self.assertEqual(status, 0)
        self.assertEqual([call.args[0] for call in mocks["run"].call_args_list],
                         [[sys.executable, "app/setup_postgres.py"]])
        self.assertTrue(self.next_steps(out).startswith("Connect an AI tool"), self.next_steps(out))
        self.assertNotRegex(self.next_steps(out), r"(?m)^\d\. ")

    def test_missing_settings_packages_and_articles_are_listed_before_indexing(self):
        # No database yet, so the index step is not reached; this Python has no packages; data/ is empty.
        status, out, mocks = self.main([""], screens={"ask_postgres": {"DATABASE_URL": ""}},
                                       where=computer("none"), here=EVERYTHING)
        self.assertEqual(status, 0)
        mocks["run"].assert_not_called()
        tail = self.next_steps(out)
        numbered = re.findall(r"(?m)^(\d)\. (.*)$", tail)
        self.assertEqual([number for number, _ in numbered], ["1", "2", "3", "4"])
        self.assertIn("DATABASE_URL", numbered[0][1])
        self.assertIn("virtual environment", numbered[1][1])
        self.assertIn("data", numbered[2][1])
        self.assertEqual(numbered[3][1], "Index your archive:")
        self.assertIn("     python app/setup_postgres.py\nConnect an AI tool", tail)
        self.assertLess(tail.index("python3"), tail.index("python app/import_archive.py --help"))
        # The wizard is run again once, and only after its environment is on: not in step 1, out here.
        self.assertEqual(tail.count("python install.py"), 1)
        self.assertLess(tail.index("source .venv/bin/activate"), tail.index("python install.py"))
        self.assertIn("type what is missing when you run this wizard again in step 2.", numbered[0][1])
        # ...and the example names the Python those steps will put the packages in.
        self.assertIn(f'command: "{install.venv_python()}"', tail)
        self.assertIn("change command: and the HTTP command", flat(tail))

    def test_a_python_without_the_packages_is_not_asked_to_index_and_the_example_says_so(self):
        self.articles()
        status, out, mocks = self.main(["", "n"], here=EVERYTHING)
        self.assertEqual(status, 0)
        mocks["run"].assert_not_called()
        self.assertIn("The indexer cannot run with this Python", out)
        tail = self.next_steps(out)
        self.assertLess(tail.index("python -m pip install -r requirements.txt"),
                        tail.index("python app/setup_postgres.py"))
        self.assertIn("will not work until the packages Dewey needs are installed", flat(tail))

    def test_the_dewey_folders_venv_is_named_when_the_wizard_ran_outside_it(self):
        self.articles()
        status, out, mocks = self.main(["", "n"], where=computer("off"), here=EVERYTHING, venv=())
        self.assertEqual(status, 0)
        mocks["run"].assert_not_called()
        tail = self.next_steps(out)
        self.assertIn("Nothing needs installing", flat(tail))
        self.assertIn("     source .venv/bin/activate\n", tail)
        self.assertNotIn("pip install", tail)
        self.assertIn(f'command: "{install.venv_python()}"', tail)
        self.assertNotIn("change command:", tail)

    def test_the_real_dot_env_is_never_touched_by_these_tests(self):
        self.assertNotEqual(install.ENV_PATH, REPO / ".env")
        self.assertEqual(install.ENV_PATH.parent, self.root)


# ------------------------------------------- what the guides say about these screens

class WrittenDownTests(TempRootTestCase):
    """The guides quote these screens, and the wizard names parts of the guides. Both have to stay
    true: people search a guide for the line they see, and follow the wizard's pointers."""

    def read(self, *path):
        return REPO.joinpath(*path).read_text(encoding="utf-8")

    def section(self, text, heading):
        """The text under a heading, up to the next heading of its level or above."""
        self.assertIn(f"\n{heading}\n", text)
        level = len(heading.split(" ")[0])
        return re.split(rf"\n#{{1,{level}}} ", text.split(f"\n{heading}\n", 1)[1])[0]

    def test_the_wizard_points_to_parts_of_the_guides_that_are_there(self):
        source = (REPO / "install.py").read_text(encoding="utf-8")
        install_guide = self.read("docs", "INSTALL.md")
        # After a pip error on a Python newer than the tested ones.
        self.assertIn("see Troubleshooting in docs/INSTALL.md", source)
        troubleshooting = self.section(install_guide, "## Troubleshooting")
        self.assertIn("| `No matching distribution found for voyageai", troubleshooting)
        # After app/setup.py has finished.
        self.assertIn("(Build the search index, in docs/AZURE.md)", source)
        self.assertRegex(self.section(self.read("docs", "AZURE.md"), "## Build the search index"),
                         r"lines containing `Error` or `Failed`")
        # The "get your articles into data/" step, and the line above the MCP example.
        self.assertIn("docs/IMPORTING.md explains how", source)
        self.assertIn("\n## Run an import\n", self.read("docs", "IMPORTING.md"))
        self.assertIn("full recipes for each client: docs/MCP.md", source)
        self.assertIn("\n## Client recipes\n", self.read("docs", "MCP.md"))

    def test_the_install_guide_quotes_what_the_wizard_prints(self):
        guide = self.section(self.read("docs", "INSTALL.md"), "### What the wizard asks")
        self.articles()
        where = computer("dewey")
        with mock.patch.object(install, "in_virtualenv", return_value=True), \
                mock.patch.object(install, "python_situation", return_value=where), \
                mock.patch.object(install, "run", return_value=False):
            with probe(here=EVERYTHING):
                _, skipped, _ = wizard(install.install_dependencies, OPEN_LOCAL, answers=["n"])
                _, pip_error, install_questions = wizard(install.install_dependencies, OPEN_LOCAL, answers=[""])
                _, refused, _ = wizard(install.index_archive, OPEN_LOCAL)
                _, example, _ = wizard(install.print_mcp_next_steps, OPEN_LOCAL, where)
            with probe(here=()):
                _, nothing_to_do, _ = wizard(install.install_dependencies, OPEN_LOCAL, answers=["n"])
                _, asked_first, index_questions = wizard(install.index_archive, OPEN_LOCAL, answers=[""])
                _, failed, _ = wizard(install.index_archive, OPEN_LOCAL, answers=["y"])
        for quoted, printed in (("This Python does not have Dewey's packages yet", skipped),
                                ("This Python already has Dewey's packages", nothing_to_do),
                                ("pip stopped with an error", pip_error),
                                ("The indexer cannot run with this Python", refused),
                                ("so it starts only if you type y.", asked_first),
                                ("The indexer stopped with an error", failed),
                                ("The commands below will not work until the packages Dewey needs are installed",
                                 example)):
            with self.subTest(quoted=quoted):
                self.assertIn(quoted, printed)
                self.assertIn(f"`{quoted}`", guide)
        self.assertIn("`Index them now? (y/N):`", guide)
        self.assertEqual(index_questions, ["Index them now? (y/N): "])
        self.assertIn("`Install requirements.txt now?`", guide)
        self.assertEqual(install_questions, ["Install requirements.txt now? (Y/n): "])
        self.assertIn("change command: and the HTTP command", flat(example))
        self.assertIn("change `command:` and the HTTP command", guide)
        # The guide's old advice, which the wizard now follows by itself.
        self.assertNotIn("Answer no if you skipped installing the packages", guide)
        self.assertNotIn("but not indexing you turned down", guide)

    def test_the_guide_names_the_indexing_commands_next_steps_lists(self):
        guide = self.section(self.read("docs", "INSTALL.md"), "### What the wizard asks")
        self.articles()
        for values, command in ((OPEN_LOCAL, "python app/setup_postgres.py"), (AZURE, "python app/setup.py")):
            with self.subTest(values=values), probe():
                (step,) = install.remaining_steps(values, [], "declined", computer("dewey"))
                self.assertEqual(commands(step), [command])
                self.assertIn(f"`{command}`", guide)

    def test_the_troubleshooting_table_has_the_lines_people_will_search_for(self):
        table = self.section(self.read("docs", "INSTALL.md"), "## Troubleshooting")
        (row,) = [line for line in table.splitlines() if "`This Python does not have Dewey's packages yet`" in line]
        self.assertIn("`The indexer cannot run with this Python`", row)
        self.assertIn("Next steps", row)

    def test_the_mcp_guide_says_which_python_the_example_names(self):
        guide = self.read("docs", "MCP.md")
        recipes = flat(self.section(guide, "## Client recipes").split("\n### ")[0])
        self.assertIn("names a Python that has Dewey's packages", recipes)
        self.assertIn("change `command:` to the Python that has them", recipes)
        self.assertNotIn("correctly if you ran it with the virtual environment on", recipes)
        # The step it points to runs .venv's own Python, so that is the Python the error is about.
        troubleshooting = self.section(guide, "## Troubleshooting")
        self.assertIn("`ModuleNotFoundError` there means `.venv`'s Python doesn't have Dewey's packages: turn the "
                      "environment on and run `python -m pip install -r requirements.txt`", troubleshooting)
        self.assertNotIn("means `command` names a Python without", troubleshooting)
        (by_hand,) = re.findall(r"\n3\. \*\*Start the server once by hand\.\*\*[\s\S]*?```bash\n(.*)\n",
                                self.section(guide, "### Before you start: stdio"))
        self.assertEqual(by_hand.strip(), '"$PWD/.venv/bin/python" "$PWD/app/mcp_server.py"')
        self.assertIn("If it couldn't check, it says so and names the Python that ran the wizard", recipes)

    def test_the_readme_says_why_the_answer_is_no_and_where_the_command_is(self):
        (paragraph,) = [line for line in self.read("README.md").splitlines()
                        if "**When it asks `Index them now?`, type `y`:**" in line]
        self.assertIn("the default is No, because indexing can cost money or take a while", paragraph)
        self.assertIn("the wizard's last screen shows the command", paragraph)


if __name__ == "__main__":
    unittest.main()

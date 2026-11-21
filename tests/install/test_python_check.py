"""
Tests for install.py's Python version check (issue #19, boxes a and g): which Pythons it stops
on, what it tells the person to do, and the two things that keep it true over time:

- the three version constants agree with requirements.txt, the CI matrix, the README and the
  install guide;
- install.py still loads on Python 3.9 (macOS's own /usr/bin/python3), because a check that
  can't run on the old Python can't say anything about it.

Most tests describe a computer (its system, and where the Python lives) and read the lines the
wizard would print, so they need no old or new interpreter. The last class runs the wizard with
real interpreters when this computer has them, against a copy of install.py in a temporary
folder. Standard library only (one test reads the installed packages' metadata and skips when
it can't); no network.
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
from unittest import mock

from tests.docs.test_docs import shell_problems

REPO = pathlib.Path(__file__).resolve().parents[2]


def load_install():
    spec = importlib.util.spec_from_file_location("dewey_install_pycheck", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


install = load_install()

ROOTS = {"mac": "/Users/you/dewey-ai", "linux": "/home/you/dewey-ai", "windows": "C:\\Users\\you\\dewey-ai"}
OLD, UNTESTED, NEW = (3, 9, 6), (3, 14, 10), (3, 15, 2)


def computer(system="mac", lives="none", in_root=True, root=None, own_venv=False):
    """A described computer. `lives` says where the Python running the wizard is:
    "dewey" (the Dewey folder's .venv), "off" (outside any environment, and the Dewey folder has
    a .venv that isn't turned on), "broken" (outside, and the Dewey folder's .venv is not a working
    environment: its Python is gone), "none" (outside, and there is no .venv yet), or the folder
    of a virtual environment somewhere else. With `own_venv`, the Dewey folder has a working .venv
    as well as that other environment."""
    root = root or ROOTS[system]
    sep = "\\" if system == "windows" else "/"
    venv = {"none": "", "off": "", "broken": "", "dewey": root + sep + ".venv"}.get(lives, lives)
    return install.PythonSituation(system=system, root=root, in_root=in_root, venv=venv,
                                   in_dewey_venv=lives == "dewey",
                                   dewey_venv_exists=own_venv or lives in ("off", "dewey"),
                                   dewey_venv_broken=lives == "broken")


OTHER_PROJECT = "/home/you/other-project/.venv"   # another project's environment, left turned on


def commands(lines, where=None):
    """The command lines among printed lines: two leading spaces, minus links and the bare folder
    of an environment that is somewhere else."""
    bare = {where.venv} if where is not None and where.venv else set()
    return [line.strip() for line in lines
            if line.startswith("  ") and not line.strip().startswith("http") and line.strip() not in bare]


def said(function, *args, **kwargs):
    """(result, printed text) of a wizard function that must not ask anything."""
    out = io.StringIO()
    no_question = AssertionError("the version check asked a question")
    with mock.patch.object(install, "_STYLED", False), contextlib.redirect_stdout(out), \
            mock.patch("builtins.input", side_effect=no_question), \
            mock.patch.object(install.getpass, "getpass", side_effect=no_question):
        result = function(*args, **kwargs)
    return result, out.getvalue()


class PythonVerdictTests(unittest.TestCase):
    def test_verdict_for_each_version(self):
        for version, verdict in [((3, 9), "old"), ((3, 10), "old"), ((3, 11), "ok"), ((3, 12), "ok"),
                                 ((3, 13), "ok"), ((3, 14), "untested"), ((3, 15), "new"), ((3, 16), "new"),
                                 ((4, 0), "new"), ((2, 7), "old")]:
            with self.subTest(version=version):
                self.assertEqual(install.python_verdict(version), verdict)
        self.assertEqual(install.python_verdict((3, 9, 6, "final", 0)), "old")   # shaped like sys.version_info
        self.assertEqual(install.python_verdict((3, 13, 17, "final", 0)), "ok")
        self.assertEqual(install.python_verdict(), install.python_verdict(sys.version_info))

    def test_the_three_bounds_are_in_order(self):
        self.assertLessEqual(install.MIN_PYTHON, install.TESTED_PYTHON)
        self.assertLess(install.TESTED_PYTHON, install.TOO_NEW_PYTHON)


class PythonReasonTests(unittest.TestCase):
    def test_too_old_and_too_new_say_which_versions_work(self):
        self.assertEqual(install.python_reason("old", OLD),
                         ["Dewey needs Python 3.11 to 3.13; this is 3.9.6, which is too old."])
        new = install.python_reason("new", NEW)
        self.assertEqual(new[0], "Dewey needs Python 3.11 to 3.13; this is 3.15.2, which is too new.")
        self.assertIn("Python 3.15", new[1])
        self.assertEqual(install.python_reason("old", sys.version_info)[0].count("; this is"), 1)

    def test_the_numbers_come_from_the_constants(self):
        with mock.patch.object(install, "TESTED_PYTHON", (3, 14)), mock.patch.object(install, "MIN_PYTHON", (3, 12)):
            for verdict, version in (("old", OLD), ("new", (3, 16, 0)), ("untested", (3, 15, 1))):
                with self.subTest(verdict=verdict):
                    text = "\n".join(install.python_reason(verdict, version))
                    self.assertIn("3.12 to 3.14", text)
                    self.assertNotIn("3.11", text)
                    self.assertNotIn("3.13", text)

    def test_untested_python_is_described_in_three_lines_not_refused(self):
        lines = install.python_reason("untested", UNTESTED)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Dewey is tested with Python 3.11 to 3.13; this is 3.14.10.")
        self.assertIn("Python 3.14", lines[1])
        self.assertIn("C compiler", lines[1])
        self.assertIn("Python 3.13 is the safest choice", lines[2])
        self.assertNotIn("needs Python", "\n".join(lines))


class EnvironmentStepsTests(unittest.TestCase):
    """environment_steps(): the lines for "make or turn on the environment, then run the wizard
    again", shared by the version check and the install step."""

    def test_an_environment_that_is_not_turned_on_is_turned_on_never_made_again(self):
        for version in (OLD, UNTESTED, NEW, (3, 12, 3)):
            with self.subTest(version=version):
                lines = install.environment_steps(computer("mac", "off"), version)
                self.assertEqual(commands(lines), ["source .venv/bin/activate", "python install.py"])
                self.assertNotIn("-m venv", "\n".join(lines))

    def test_no_environment_yet_makes_one_without_clear(self):
        lines = install.environment_steps(computer("mac", "none"), OLD)
        self.assertEqual(commands(lines),
                         ["python3.13 -m venv .venv", "source .venv/bin/activate", "python install.py"])
        self.assertNotIn("--clear", "\n".join(lines))

    def test_a_dot_venv_that_is_not_a_working_environment_is_made_again_with_clear(self):
        # Without --clear, venv keeps the old links in the folder, so `python` there stays broken
        # (RealInterpreterTests shows it with a real environment).
        for version, python in ((OLD, "python3.13"), (NEW, "python3.13"), ((3, 12, 3), "python3.12")):
            with self.subTest(version=version):
                lines = install.environment_steps(computer("mac", "broken"), version)
                self.assertEqual(commands(lines), [f"{python} -m venv --clear .venv", "source .venv/bin/activate",
                                                   "python install.py"])
                before_clear = lines[[line.strip() for line in lines].index(f"{python} -m venv --clear .venv") - 1]
                self.assertIn("deletes what is in .venv", before_clear)

    def test_another_environment_is_on_and_the_dewey_folder_has_its_own(self):
        # Another project's environment was left turned on. The answer is to turn Dewey's on,
        # never a command that empties the other project's.
        for version in (OLD, UNTESTED, NEW, (3, 12, 3)):
            with self.subTest(version=version):
                lines = install.environment_steps(computer("linux", OTHER_PROJECT, own_venv=True), version)
                self.assertEqual(commands(lines), ["source .venv/bin/activate", "python install.py"])
                text = "\n".join(lines)
                self.assertNotIn("-m venv", text)
                self.assertNotIn("other-project", text)
        windows = install.environment_steps(computer("windows", "C:\\Users\\you\\other\\.venv", own_venv=True), OLD)
        self.assertEqual(commands(windows), [".venv\\Scripts\\Activate.ps1", "python install.py"])

    def test_the_dewey_folders_environment_is_made_again_and_the_text_says_what_is_deleted(self):
        lines = install.environment_steps(computer("mac", "dewey"), OLD)
        self.assertEqual(commands(lines),
                         ["python3.13 -m venv --clear .venv", "source .venv/bin/activate", "python install.py"])
        before_clear = lines[[line.strip() for line in lines].index("python3.13 -m venv --clear .venv") - 1]
        self.assertIn("deletes what is in .venv", before_clear)

    def test_an_environment_in_another_folder_is_made_again_at_its_own_path(self):
        where = computer("linux", "/home/you/envs/dewey")
        lines = install.environment_steps(where, NEW)
        self.assertEqual(commands(lines), ["python3.13 -m venv --clear /home/you/envs/dewey",
                                           "source /home/you/envs/dewey/bin/activate", "python install.py"])
        self.assertNotIn(".venv/bin/activate", "\n".join(lines))
        spaced = install.environment_steps(computer("mac", "/Users/you/My Envs/dewey"), OLD)
        self.assertEqual(commands(spaced)[:2], ["python3.13 -m venv --clear '/Users/you/My Envs/dewey'",
                                                "source '/Users/you/My Envs/dewey/bin/activate'"])

    def test_the_commands_name_this_python_when_dewey_is_tested_with_it(self):
        # The install step prints these lines for a Python that is fine but outside an environment:
        # on Ubuntu 24.04 that is python3.12, and python3.13 may not exist there.
        lines = install.environment_steps(computer("linux", "none"), (3, 12, 3))
        self.assertEqual(commands(lines)[0], "python3.12 -m venv .venv")
        self.assertNotIn("3.13", "\n".join(lines))
        self.assertEqual(commands(install.environment_steps(computer("windows", "none"), (3, 11, 9)))[0],
                         "py -3.11 -m venv .venv")
        # Not a Python Dewey is tested with: the newest tested one, from the constant.
        for version in (OLD, UNTESTED, NEW):
            with self.subTest(version=version):
                self.assertEqual(commands(install.environment_steps(computer("mac", "none"), version))[0],
                                 "python3.13 -m venv .venv")
        with mock.patch.object(install, "TESTED_PYTHON", (3, 12)):
            self.assertEqual(commands(install.environment_steps(computer("mac", "dewey"), OLD))[0],
                             "python3.12 -m venv --clear .venv")

    def test_an_environment_whose_python_is_fine_is_never_made_again(self):
        # Nothing asks for the steps there today. If something ever does, the answer must not be a
        # command that empties a working environment.
        for lives in ("dewey", "/home/you/envs/dewey"):
            with self.subTest(lives=lives):
                lines = install.environment_steps(computer("linux", lives), (3, 12, 3))
                self.assertEqual(commands(lines), ["python install.py"])

    def test_windows_gets_the_powershell_commands(self):
        lines = install.environment_steps(computer("windows", "dewey"), OLD)
        self.assertEqual(commands(lines),
                         ["py -3.13 -m venv --clear .venv", ".venv\\Scripts\\Activate.ps1", "python install.py"])
        text = "\n".join(lines)
        self.assertNotIn("source ", text)
        self.assertNotIn("python3.13", text)
        off = install.environment_steps(computer("windows", "off"), OLD)
        self.assertEqual(commands(off), [".venv\\Scripts\\Activate.ps1", "python install.py"])
        elsewhere = install.environment_steps(computer("windows", "C:\\Users\\you\\My Envs\\dewey"), OLD)
        self.assertEqual(commands(elsewhere)[:2], ["py -3.13 -m venv --clear 'C:\\Users\\you\\My Envs\\dewey'",
                                                   "& 'C:\\Users\\you\\My Envs\\dewey\\Scripts\\Activate.ps1'"])

    def test_linux_is_told_other_versions_work_and_to_update_pip_before_the_last_command(self):
        for lives in ("none", "dewey", "/home/you/envs/dewey"):
            with self.subTest(lives=lives):
                lines = install.environment_steps(computer("linux", lives), OLD)
                (any_version,) = [i for i, line in enumerate(lines) if "3.11 to 3.13 works" in line]
                (pip,) = [i for i, line in enumerate(lines) if "pip" in line]
                venv = next(i for i, line in enumerate(lines) if "-m venv" in line)
                self.assertLess(any_version, venv)
                self.assertIn("in place of 3.13", lines[any_version])
                self.assertIn("step 4 of Create a virtual environment in docs/INSTALL.md", lines[pip])
                self.assertEqual(lines[-1].strip(), "python install.py")
                self.assertLess(venv, pip)
                self.assertLess(pip, len(lines) - 1)
        # Turning on an environment that already exists makes nothing, so neither line applies. On
        # macOS and Windows the guide has no pip step ("Create a virtual environment", step 4).
        for where in (computer("linux", "off"), computer("mac", "none"), computer("mac", "dewey"),
                      computer("windows", "dewey")):
            text = "\n".join(install.environment_steps(where, OLD))
            self.assertNotIn("pip", text)
            self.assertNotIn("works", text)

    def test_cd_comes_first_when_the_terminal_is_somewhere_else(self):
        # The other commands are relative: `--clear .venv` in another folder would empty that
        # folder's environment.
        for lives in ("none", "off", "broken", "dewey", "/home/you/envs/dewey"):
            with self.subTest(lives=lives):
                away = install.environment_steps(computer("linux", lives, in_root=False), OLD)
                self.assertEqual(commands(away)[0], "cd /home/you/dewey-ai")
                here = install.environment_steps(computer("linux", lives, in_root=True), OLD)
                self.assertNotIn("cd ", "\n".join(here))
                asked = install.environment_steps(computer("linux", lives, in_root=True), OLD, cd_first=True)
                self.assertEqual(commands(asked)[0], "cd /home/you/dewey-ai")
        spaced = computer("mac", "dewey", in_root=False, root="/Users/you/My Projects/dewey-ai")
        self.assertEqual(commands(install.environment_steps(spaced, OLD))[0], "cd '/Users/you/My Projects/dewey-ai'")
        windows = computer("windows", "dewey", in_root=False, root="C:\\Users\\Jo Doe\\dewey-ai")
        self.assertEqual(commands(install.environment_steps(windows, OLD))[0], "cd 'C:\\Users\\Jo Doe\\dewey-ai'")
        self.assertEqual(commands(install.environment_steps(computer("windows", "off", in_root=False), OLD))[0],
                         "cd C:\\Users\\you\\dewey-ai")

    def test_every_command_is_on_its_own_line_under_a_line_that_says_what_it_does(self):
        for system in ("mac", "linux", "windows"):
            for lives in ("none", "off", "broken", "dewey", "/somewhere/else/env"):
                lines = install.environment_steps(computer(system, lives, in_root=False), OLD)
                with self.subTest(system=system, lives=lives):
                    self.assertFalse(lines[0].startswith("  "))
                    for i, line in enumerate(lines):
                        if line.startswith("  "):
                            self.assertFalse(lines[i - 1].startswith("  "), f"two commands in a row: {line!r}")
                            self.assertTrue(lines[i - 1].rstrip().endswith(":"), lines[i - 1])
                    self.assertTrue(lines[-1].startswith("  "))  # nothing to read after the last command

    def test_defaults_describe_this_computer_and_this_python(self):
        where = computer("mac", "none")
        with mock.patch.object(install, "python_situation", return_value=where) as situation:
            self.assertEqual(install.environment_steps(), install.environment_steps(where, sys.version_info))
        situation.assert_called()


class SwitchPythonLinesTests(unittest.TestCase):
    """What the wizard says after "too old" or "too new", by where the Python lives."""

    def test_too_old_in_deweys_venv_shows_the_commands_that_make_it_again(self):
        # Box (a).
        where = computer("mac", "dewey")
        lines = install.switch_python_lines(where, OLD)
        self.assertEqual(commands(lines), ["cd /Users/you/dewey-ai", "python3.13 -m venv --clear .venv",
                                           "source .venv/bin/activate", "python install.py"])
        text = "\n".join(lines)
        self.assertIn("This Python is the one in Dewey's virtual environment (the .venv folder)", text)
        self.assertIn("has to be made again", text)
        self.assertIn("deletes what is in .venv", text)
        self.assertIn("  " + install.LINKS["python"], lines)
        self.assertIn("Recreate the virtual environment", lines[-1])

    def test_windows_gets_the_powershell_commands(self):
        lines = install.switch_python_lines(computer("windows", "dewey"), OLD)
        self.assertEqual(commands(lines), ["cd C:\\Users\\you\\dewey-ai", "py -3.13 -m venv --clear .venv",
                                           ".venv\\Scripts\\Activate.ps1", "python install.py"])
        text = "\n".join(lines)
        self.assertIn("PowerShell", text)
        self.assertNotIn("source ", text)
        self.assertNotIn("python3.13", text)
        self.assertIn("PowerShell", "\n".join(install.switch_python_lines(computer("windows", "off"), OLD)))

    def test_a_dot_venv_that_is_not_turned_on_is_never_deleted(self):
        for system, activate in (("mac", "source .venv/bin/activate"), ("linux", "source .venv/bin/activate"),
                                 ("windows", ".venv\\Scripts\\Activate.ps1")):
            for version in (OLD, NEW):
                with self.subTest(system=system, version=version):
                    lines = install.switch_python_lines(computer(system, "off"), version)
                    self.assertEqual(commands(lines), [activate, "python install.py"])
                    text = "\n".join(lines)
                    self.assertIn("not turned on", text)
                    for never in ("-m venv", "--clear", "install it first", "install one first", "python.org"):
                        self.assertNotIn(never, text)

    def test_another_projects_environment_is_never_emptied_when_dewey_has_its_own(self):
        # This Python belongs to another project's environment, left turned on, and the Dewey
        # folder has a working .venv: turn that on. Nothing names the other project's folder.
        for version in (OLD, NEW):
            with self.subTest(version=version):
                where = computer("linux", OTHER_PROJECT, own_venv=True)
                lines = install.switch_python_lines(where, version)
                self.assertEqual(commands(lines, where), ["source .venv/bin/activate", "python install.py"])
                text = "\n".join(lines)
                self.assertIn("not turned on", text)
                for never in ("-m venv", "--clear", "other-project"):
                    self.assertNotIn(never, text)

    def test_a_dot_venv_that_is_not_a_working_environment_is_made_again(self):
        lines = install.switch_python_lines(computer("mac", "broken"), OLD)
        self.assertEqual(commands(lines), ["cd /Users/you/dewey-ai", "python3.13 -m venv --clear .venv",
                                           "source .venv/bin/activate", "python install.py"])
        text = "\n".join(lines)
        self.assertIn("not a working virtual environment", text)
        self.assertIn("deletes what is in .venv", text)
        self.assertIn("Recreate the virtual environment", lines[-1])

    def test_no_environment_yet_makes_one_without_clear(self):
        lines = install.switch_python_lines(computer("mac", "none"), OLD)
        self.assertEqual(commands(lines), ["cd /Users/you/dewey-ai", "python3.13 -m venv .venv",
                                           "source .venv/bin/activate", "python install.py"])
        self.assertNotIn("--clear", "\n".join(lines))
        self.assertIn("Create a virtual environment", lines[-1])
        self.assertNotIn("Recreate", lines[-1])

    def test_an_environment_in_another_folder_is_named_and_made_again_there(self):
        where = computer("linux", "/home/you/envs/dewey")
        lines = install.switch_python_lines(where, OLD)
        self.assertIn("  /home/you/envs/dewey", lines)
        self.assertEqual(commands(lines, where),
                         ["cd /home/you/dewey-ai", "python3.13 -m venv --clear /home/you/envs/dewey",
                          "source /home/you/envs/dewey/bin/activate", "python install.py"])
        self.assertIn("that environment has to be made again", "\n".join(lines))

    def test_each_system_gets_only_its_own_way_to_install_python(self):
        mac = "\n".join(install.switch_python_lines(computer("mac", "none"), OLD))
        linux = "\n".join(install.switch_python_lines(computer("linux", "none"), OLD))
        windows = "\n".join(install.switch_python_lines(computer("windows", "none"), OLD))
        self.assertIn("macOS installer", mac)
        self.assertIn("Windows installer (64-bit)", windows)
        self.assertIn("package manager", linux)
        for text in (mac, windows):
            self.assertIn(install.LINKS["python"], text)
            self.assertIn("If Python 3.13 is not installed yet", text)
            self.assertNotIn("package manager", text)
        self.assertNotIn("Windows installer", mac)
        self.assertNotIn("macOS installer", windows)
        self.assertNotIn("python.org", linux)
        self.assertIn("3.11 to 3.13", linux)   # Ubuntu 24.04's own Python is 3.12, and it works
        self.assertIn("Get your Linux computer ready", linux)

    def test_steps_that_start_with_installing_python_always_begin_with_cd(self):
        # After installing Python the guide has people open a new terminal window, which starts in
        # the home folder: `--clear .venv` there would empty the wrong environment.
        for system in ("mac", "linux", "windows"):
            for lives in ("none", "broken", "dewey", "/somewhere/else/env"):
                with self.subTest(system=system, lives=lives):
                    where = computer(system, lives, in_root=True)
                    self.assertTrue(commands(install.switch_python_lines(where, OLD), where)[0].startswith("cd "))

    def test_the_guide_has_the_sections_the_wizard_names(self):
        guide = (REPO / "docs" / "INSTALL.md").read_text(encoding="utf-8")

        def section(heading):
            self.assertIn(f"\n## {heading}\n", guide)
            return guide.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]

        said_somewhere = "\n".join(
            line for system in ("mac", "linux", "windows") for lives in ("none", "off", "broken", "dewey", "/e")
            for version in (OLD, UNTESTED, NEW)
            for line in install.python_check_lines(version, computer(system, lives)))
        create, recreate = "Create a virtual environment", "Recreate the virtual environment"
        for phrase, heading, has in (
                (f"For more help: {create}, in docs/INSTALL.md.", create, ""),
                (f"step 4 of {create} in docs/INSTALL.md", create, "\n4. **On Linux, update pip.**"),
                (f"For more help: {recreate}, under Troubleshooting in docs/INSTALL.md.",
                 "Troubleshooting", f"\n**{recreate}.**"),
                ("see Troubleshooting in docs/INSTALL.md", "Troubleshooting", "`Dewey is tested with Python "),
                ("(Get your Linux computer ready, step 3)", "Get your Linux computer ready",
                 "\n3. **Install Python.**")):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, said_somewhere)
                self.assertTrue(has in section(heading), f"docs/INSTALL.md's {heading!r} section no longer has "
                                                         f"{has!r}, which the wizard points to")


class PrintedCommandsPasteSafelyTests(unittest.TestCase):
    """CLAUDE.md: commands Dewey prints must paste safely into an interactive zsh."""

    DANGEROUS = set("'\"`;#()<>|&$!*?[]{}\\")
    # A Mac's file system ignores case, so its zsh runs `Make a ...` as make and `More help` as more
    # (seen on macOS 26). A line of prose must not start with a word that is also a common command.
    ALSO_COMMANDS = {"apply", "at", "cat", "cd", "cp", "cut", "date", "diff", "do", "echo", "env", "exec", "export",
                     "file", "find", "fold", "from", "git", "go", "head", "host", "info", "install", "join", "kill",
                     "last", "leave", "less", "link", "ln", "look", "mail", "make", "man", "more", "mv", "open",
                     "paste", "patch", "pip", "print", "python", "read", "rm", "say", "script", "set", "sort",
                     "source", "split", "sudo", "tail", "test", "time", "top", "touch", "type", "wait", "what",
                     "which", "who", "write", "yes", "zip"}

    def situations(self):
        for system in ("mac", "linux"):
            for lives in ("none", "off", "broken", "dewey", "/home/you/envs/dewey", "/home/you/my envs/dewey's [old]"):
                for in_root in (True, False):
                    for root in (None, "/Users/you/My Projects/dewey-ai (copy)"):
                        yield computer(system, lives, in_root=in_root, root=root)
                        if lives.startswith("/"):
                            yield computer(system, lives, in_root=in_root, root=root, own_venv=True)

    def test_printed_commands_paste_safely_into_zsh(self):
        checked = 0
        for where in self.situations():
            for version in (OLD, UNTESTED, NEW, (3, 12, 3)):
                for lines in (install.python_check_lines(version, where), install.environment_steps(where, version)):
                    for command in commands(lines, where):
                        with self.subTest(where=where, version=version, command=command):
                            self.assertEqual(shell_problems(command), [])
                            checked += 1
        self.assertGreater(checked, 100)

    def test_text_after_the_first_command_does_no_harm_if_it_is_pasted_too(self):
        # A person may select the whole block. Between and after the commands there are no quotes,
        # apostrophes, ; # or other characters a shell acts on.
        for where in self.situations():
            for version in (OLD, UNTESTED, NEW):
                lines = install.python_check_lines(version, where)
                first = next((i for i, line in enumerate(lines)
                              if line.startswith("  ") and line.strip() in commands(lines, where)), None)
                if first is None:
                    continue
                for line in lines[first:]:
                    if line.strip() and not line.startswith("  "):
                        with self.subTest(where=where, version=version, line=line):
                            self.assertEqual(self.DANGEROUS & set(line), set())
                            self.assertNotIn(line.split()[0].lower().rstrip(":,."), self.ALSO_COMMANDS)


class CheckPythonTests(unittest.TestCase):
    def test_supported_versions_say_nothing_and_carry_on(self):
        with mock.patch.object(install, "python_situation", side_effect=AssertionError("not needed")):
            for version in ((3, 11, 0), (3, 12, 6), (3, 13, 17)):
                with self.subTest(version=version):
                    self.assertEqual(said(install.check_python, version), (True, ""))

    def test_too_old_and_too_new_stop_without_asking(self):
        for version, word in ((OLD, "too old"), (NEW, "too new")):
            for lives in ("none", "broken", "dewey", "/home/you/envs/dewey"):
                with self.subTest(version=version, lives=lives):
                    carry_on, out = said(install.check_python, version, computer("linux", lives))
                    self.assertIs(carry_on, False)
                    self.assertIn(f"Dewey needs Python 3.11 to 3.13; this is {'.'.join(map(str, version))}, "
                                  f"which is {word}.", out)
                    self.assertIn("  python3.13 -m venv ", out)
                    self.assertIn("  python install.py\n", out)

    def test_the_not_turned_on_check_comes_first_for_every_verdict(self):
        # Nobody whose .venv is fine, and merely not turned on, is told to make it again. That
        # includes someone who left another project's environment on in this terminal window.
        for version in (OLD, UNTESTED, NEW):
            for where in (computer("mac", "off"), computer("mac", "/Users/you/other-project/.venv", own_venv=True)):
                with self.subTest(version=version, where=where):
                    _, out = said(install.check_python, version, where)
                    self.assertIn("not turned on in this terminal window", out)
                    self.assertIn("  source .venv/bin/activate\n", out)
                    self.assertNotIn("-m venv", out)
                    self.assertNotIn("python.org", out)
                    self.assertNotIn("other-project", out)

    def test_untested_python_gets_three_lines_and_carries_on(self):
        for lives in ("none", "broken", "dewey", "/home/you/envs/dewey"):
            with self.subTest(lives=lives):
                carry_on, out = said(install.check_python, UNTESTED, computer("linux", lives))
                self.assertIs(carry_on, True)
                self.assertEqual(out.strip("\n").split("\n"), install.python_reason("untested", UNTESTED))
                self.assertNotIn("-m venv", out)

    def test_untested_python_outside_an_environment_that_exists_is_told_and_carries_on(self):
        carry_on, out = said(install.check_python, UNTESTED, computer("linux", "off"))
        self.assertIs(carry_on, True)
        self.assertIn("Dewey is tested with Python 3.11 to 3.13; this is 3.14.10.", out)
        self.assertIn("Control+C", out)
        self.assertIn("carries on with Python 3.14", out)

    def test_the_situation_is_read_from_this_computer_when_none_is_given(self):
        with mock.patch.object(install, "python_situation", return_value=computer("mac", "dewey")) as situation:
            carry_on, out = said(install.check_python, OLD)
        situation.assert_called_once_with()
        self.assertIs(carry_on, False)
        self.assertIn("--clear .venv", out)

    def test_main_stops_before_reading_env_or_asking_anything(self):
        # Box (g): "before the first question".
        stop = AssertionError("main() went on after the version check said stop")
        with mock.patch.object(install, "check_python", return_value=False) as check, \
                mock.patch.object(install, "read_env", side_effect=stop), \
                mock.patch.object(install, "ask_stack", side_effect=stop), \
                mock.patch.object(sys, "argv", ["install.py", "--no-browser"]):
            status, out = said(install.main)
        self.assertEqual(status, 1)
        check.assert_called_once_with()
        self.assertIn("DEWEY", out)   # the banner comes first, as before

    def test_main_goes_on_to_the_first_question_when_the_check_passes(self):
        class Reached(Exception):
            pass

        with mock.patch.object(install, "check_python", return_value=True), \
                mock.patch.object(install, "read_env", return_value={}), \
                mock.patch.object(install, "ask_stack", side_effect=Reached), \
                mock.patch.object(sys, "argv", ["install.py", "--no-browser"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(Reached):
                install.main()


class InstallStepHintTests(unittest.TestCase):
    """Outside a virtual environment, the install step prints the same shared steps: this
    system's commands only, one per line, naming a Python that fits."""

    def hint(self, where):
        out = io.StringIO()
        with mock.patch.object(install, "_STYLED", False), contextlib.redirect_stdout(out), \
                mock.patch.object(install, "in_virtualenv", return_value=False), \
                mock.patch.object(install, "python_situation", return_value=where), \
                mock.patch("builtins.input", return_value="") as asked, \
                mock.patch.object(install, "run", side_effect=AssertionError("Enter means no: nothing is installed")):
            install.install_dependencies({"DEWEY_STACK": "open"})
        self.assertEqual(asked.call_count, 1)
        self.assertIn("Install into the current Python anyway?", asked.call_args[0][0])
        return out.getvalue()

    def test_the_hint_is_the_shared_steps(self):
        for where in (computer("mac", "none"), computer("linux", "none", in_root=False), computer("windows", "none"),
                      computer("mac", "off"), computer("windows", "off"), computer("linux", "broken")):
            with self.subTest(where=where):
                out = self.hint(where)
                self.assertIn("You're not in a virtual environment.", out)   # docs/INSTALL.md quotes this
                self.assertIn("\n".join(install.environment_steps(where)) + "\n", out)
                self.assertNotIn("&&", out)            # it used to chain two commands on one line...
                self.assertNotIn("(Windows:", out)     # ...and print both systems' commands

    def test_an_environment_that_exists_is_turned_on_not_made(self):
        out = self.hint(computer("mac", "off"))
        self.assertIn("not turned on in this terminal window", out)
        self.assertIn("  source .venv/bin/activate\n", out)
        self.assertNotIn("-m venv", out)
        self.assertIn("-m venv .venv\n", self.hint(computer("mac", "none")))

    def test_a_dot_venv_that_is_not_a_working_environment_is_made_again(self):
        out = self.hint(computer("mac", "broken"))
        self.assertIn("not a working virtual environment", out)
        self.assertIn("-m venv --clear .venv\n", out)
        self.assertIn("deletes what is in .venv", out)


class PythonSituationTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="dewey-pycheck-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        patch = mock.patch.object(install, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def make_venv(self, folder, with_python=True):
        python = install.venv_python() if folder == self.root / ".venv" else folder / "bin" / "python"
        python.parent.mkdir(parents=True)
        (folder / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        if with_python:
            python.write_text("", encoding="utf-8")

    def situation(self, prefix, base="/usr", cwd=None):
        with mock.patch.object(sys, "prefix", str(prefix)), mock.patch.object(sys, "base_prefix", base), \
                mock.patch.object(install.Path, "cwd", return_value=pathlib.Path(cwd or self.root)):
            return install.python_situation()

    def test_in_virtualenv(self):
        with mock.patch.object(sys, "prefix", "/a/.venv"), mock.patch.object(sys, "base_prefix", "/usr"):
            self.assertIs(install.in_virtualenv(), True)
        with mock.patch.object(sys, "prefix", "/usr"), mock.patch.object(sys, "base_prefix", "/usr"):
            self.assertIs(install.in_virtualenv(), False)
        self.assertIs(install.in_virtualenv(), sys.prefix != sys.base_prefix)

    def test_venv_python_is_where_each_system_puts_it(self):
        self.assertEqual(install.venv_python(windows=False), self.root / ".venv" / "bin" / "python")
        self.assertEqual(install.venv_python(windows=True), self.root / ".venv" / "Scripts" / "python.exe")
        self.assertEqual(install.venv_python(), install.venv_python(windows=os.name == "nt"))

    def test_running_in_the_dewey_folders_environment(self):
        self.make_venv(self.root / ".venv")
        where = self.situation(self.root / ".venv")
        self.assertEqual(where.venv, str(self.root / ".venv"))
        self.assertIs(where.in_dewey_venv, True)
        self.assertIs(where.dewey_venv_exists, True)
        self.assertEqual(where.root, str(self.root))
        self.assertIs(where.in_root, True)
        expected = "windows" if os.name == "nt" else ("mac" if sys.platform == "darwin" else "linux")
        self.assertEqual(where.system, expected)

    def test_outside_any_environment(self):
        where = self.situation("/usr")
        self.assertEqual((where.venv, where.in_dewey_venv, where.dewey_venv_exists, where.dewey_venv_broken),
                         ("", False, False, False))
        self.make_venv(self.root / ".venv")
        where = self.situation("/usr")
        self.assertEqual((where.venv, where.in_dewey_venv, where.dewey_venv_exists, where.dewey_venv_broken),
                         ("", False, True, False))

    def test_an_environment_somewhere_else(self):
        other = self.root / "envs" / "dewey"
        self.make_venv(other)
        where = self.situation(other)
        self.assertEqual((where.venv, where.in_dewey_venv, where.dewey_venv_exists), (str(other), False, False))
        # ...and when the Dewey folder has its own as well, both are reported.
        self.make_venv(self.root / ".venv")
        where = self.situation(other)
        self.assertEqual((where.venv, where.in_dewey_venv, where.dewey_venv_exists, where.dewey_venv_broken),
                         (str(other), False, True, False))

    def test_a_dot_venv_without_a_python_is_not_a_working_environment(self):
        # A leftover folder, or one whose Python was uninstalled: "turn it on" would lead nowhere,
        # and making an environment in it without --clear would keep its dead links.
        (self.root / ".venv").mkdir()
        where = self.situation("/usr")
        self.assertEqual((where.dewey_venv_exists, where.dewey_venv_broken), (False, True))
        shutil.rmtree(self.root / ".venv")
        self.make_venv(self.root / ".venv", with_python=False)
        where = self.situation("/usr")
        self.assertEqual((where.dewey_venv_exists, where.dewey_venv_broken), (False, True))
        if os.name != "nt":
            # The Python it was made with has been uninstalled: its link points nowhere.
            os.symlink("/nowhere/python3.13", str(install.venv_python()))
            where = self.situation("/usr")
            self.assertEqual((where.dewey_venv_exists, where.dewey_venv_broken), (False, True))

    def test_the_terminals_folder(self):
        self.assertIs(self.situation("/usr", cwd=self.root).in_root, True)
        self.assertIs(self.situation("/usr", cwd=self.root.parent).in_root, False)
        with mock.patch.object(install.Path, "cwd", side_effect=FileNotFoundError("the folder was deleted")):
            self.assertIs(install.python_situation().in_root, False)


REQUIREMENT_LINE = re.compile(r"(?P<name>[A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(?P<version>[^\s;]+)"
                              r"(?:\s*;\s*(?P<marker>.+))?")


class BoundsTests(unittest.TestCase):
    """The three constants are typed once, in install.py. These fail when something they describe
    moves: a pin's supported Pythons, the CI matrix, or the versions the guides name."""

    def test_bounds_match_what_the_pinned_packages_accept(self):
        try:
            from importlib import metadata

            from packaging.markers import default_environment
            from packaging.requirements import Requirement
            from packaging.specifiers import SpecifierSet
        except ImportError:
            self.skipTest("needs the packaging module (it comes with requirements.txt)")

        env = default_environment()
        pins, elsewhere = [], []
        for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = REQUIREMENT_LINE.fullmatch(line)
            self.assertTrue(m, f"not an exact pin: {line!r}")
            marker = Requirement(f"x; {m['marker']}").marker if m["marker"] else None
            if marker is not None and not marker.evaluate(env):
                elsewhere.append(m["name"])  # for another system or Python: not installed here, so not read
                continue
            try:
                dist = metadata.distribution(m["name"])
            except metadata.PackageNotFoundError:
                self.skipTest(f"requirements.txt is not installed here ({m['name']} is missing)")
            if dist.version != m["version"]:
                self.skipTest(f"requirements.txt is not installed here at its pinned versions "
                              f"({m['name']} is {dist.version}, pinned {m['version']})")
            pins.append((m["name"], marker, SpecifierSet(dist.metadata.get("Requires-Python") or "")))
        self.assertGreater(len(pins), 50)

        def blockers(minor):
            target = {**env, "python_version": f"3.{minor}", "python_full_version": f"3.{minor}.0"}
            return [name for name, marker, accepts in pins
                    if (marker is None or marker.evaluate(target))
                    and not accepts.contains(f"3.{minor}.0", prereleases=True)]

        scanned = range(7, 30)
        installable = [minor for minor in scanned if not blockers(minor)]
        note = (f" (pins not read, because they are for another system or Python: {', '.join(elsewhere) or 'none'})")
        self.assertTrue(installable, "no Python 3 version satisfies every pin" + note)
        first, last = installable[0], installable[-1]
        self.assertEqual(installable, list(range(first, last + 1)),
                         "the Pythons the pins accept are not one range" + note)
        self.assertEqual(install.MIN_PYTHON, (3, first),
                         f"MIN_PYTHON is out of date: Python 3.{first - 1} is refused by {blockers(first - 1)}" + note)
        self.assertLess(last, scanned[-1], "no pin sets an upper limit any more: TOO_NEW_PYTHON (and the "
                        "wording around it in install.py and the guides) needs another look" + note)
        self.assertEqual(install.TOO_NEW_PYTHON, (3, last + 1), f"TOO_NEW_PYTHON is out of date: Python 3.{last + 1} "
                         f"is refused by {blockers(last + 1)}" + note)

    def test_tested_range_is_what_ci_runs(self):
        workflow = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        (matrix,) = re.findall(r"^\s*python-version:\s*\[([^\]]*)\]", workflow, re.MULTILINE)
        ci = [tuple(int(n) for n in v.split(".")) for v in re.findall(r"\d+\.\d+", matrix)]
        self.assertEqual(install.MIN_PYTHON[0], install.TESTED_PYTHON[0])
        tested = [(install.MIN_PYTHON[0], minor)
                  for minor in range(install.MIN_PYTHON[1], install.TESTED_PYTHON[1] + 1)]
        self.assertEqual(ci, tested, "MIN_PYTHON to TESTED_PYTHON in install.py and the CI matrix must name the "
                                     "same versions")

    def test_readme_install_guide_and_link_name_the_same_versions(self):
        low, high = "%d.%d" % install.MIN_PYTHON, "%d.%d" % install.TESTED_PYTHON
        too_new = "%d.%d" % install.TOO_NEW_PYTHON
        minors = ["%d.%d" % (install.MIN_PYTHON[0], m)
                  for m in range(install.MIN_PYTHON[1], install.TESTED_PYTHON[1] + 1)]
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        guide = (REPO / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        glossary = (REPO / "docs" / "GLOSSARY.md").read_text(encoding="utf-8")
        self.assertIn(f"Python-{low}%E2%80%93{high}-blue", readme)                      # the badge image
        self.assertIn(f"[![Python {low}\u2013{high}]", readme)                          # its text
        self.assertIn(f"{', '.join(minors[:-1])} or {minors[-1]}.**", readme)           # "What you need"
        self.assertIn(f"{too_new} doesn't work", readme)
        self.assertIn(f"Dewey works with Python {low} to {high}.", guide)
        self.assertIn(f"{', '.join(minors[:-1])} and {minors[-1]} work", glossary)
        self.assertIn(f"{too_new} can't install Dewey", glossary)
        # The guides and the wizard send people to one installer page, for the newest tested Python.
        self.assertIn("/python-%d%d" % install.TESTED_PYTHON, install.LINKS["python"])
        self.assertIn(install.LINKS["python"], guide)

    def test_the_guide_quotes_the_wizards_own_lines(self):
        # People search the Troubleshooting table for the line they see.
        guide = (REPO / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        too_old = install.python_reason("old", OLD)[0]
        self.assertGreaterEqual(guide.count(f"`{too_old}`"), 2, "What the wizard asks, and the Troubleshooting table")
        self.assertIn("`which is too new.`", guide)
        self.assertTrue(install.python_reason("new", NEW)[0].endswith("which is too new."))
        self.assertIn(f"`{install.python_reason('untested', UNTESTED)[0]}`", guide)


ADDED_AFTER_3_9 = {"tomllib", "annotationlib", "compression", "profiling"}  # top-level standard-library modules
# Names people reach for that Python 3.9 doesn't have. Not a complete list: only a real Python 3.9
# can say for sure, which RealInterpreterTests asks when this computer has one.
NAMES_ADDED_AFTER_3_9 = {
    "typing": {"Self", "TypeAlias", "ParamSpec", "Concatenate", "TypeGuard", "Never", "LiteralString", "Required",
               "NotRequired", "Unpack", "TypeVarTuple", "override", "assert_never"},
    "datetime": {"UTC"},
    "itertools": {"pairwise", "batched"},
    "enum": {"StrEnum"},
    "dataclasses": {"KW_ONLY"},
    "types": {"UnionType", "NoneType", "EllipsisType"},
    "contextlib": {"chdir", "aclosing"},
}


class LoadsOnPython39Tests(unittest.TestCase):
    """Static checks, run on every Python CI uses. install.py must load on Python 3.9: Apple's
    /usr/bin/python3 is 3.9, and the version check is what tells those people what to do."""

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO / "install.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def module_level_imports(self):
        """Import statements that run when the file loads: not the ones inside functions."""
        found, todo = [], list(self.tree.body)
        while todo:
            node = todo.pop()
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.append(node)
            elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                todo.extend(ast.iter_child_nodes(node))
        return sorted(found, key=lambda node: node.lineno)

    def test_no_syntax_newer_than_python_3_9(self):
        try:
            ast.parse(self.source, feature_version=(3, 9))
        except SyntaxError as e:
            self.fail(f"install.py line {e.lineno} uses syntax Python 3.9 doesn't have ({e.msg}). It must load there "
                      "so that its version check can run.")

    def test_no_union_written_with_a_bar_in_annotations(self):
        # `def f(x: str | None)` parses as 3.9 syntax but fails when Python 3.9 defines the function.
        def annotations():
            for node in ast.walk(self.tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = node.args
                    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                        if arg is not None and arg.annotation is not None:
                            yield arg.annotation
                    if node.returns is not None:
                        yield node.returns
                elif isinstance(node, ast.AnnAssign):
                    yield node.annotation

        for annotation in annotations():
            for node in ast.walk(annotation):
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                    self.fail(f"install.py line {node.lineno}: write Optional[X] or Union[X, Y], not `X | Y`: "
                              "Python 3.9 can't evaluate it, and install.py must load there")

    @unittest.skipUnless(hasattr(sys, "stdlib_module_names"), "sys.stdlib_module_names needs Python 3.10+")
    def test_module_level_imports_are_standard_library_modules_python_3_9_has(self):
        imports = self.module_level_imports()
        self.assertGreater(len(imports), 10)
        for node in imports:
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, f"line {node.lineno}: install.py is run as a script")
                modules = [node.module]
            else:
                modules = [alias.name for alias in node.names]
            for module in modules:
                top = module.split(".")[0]
                with self.subTest(line=node.lineno, module=module):
                    self.assertIn(top, sys.stdlib_module_names,
                                  "install.py runs before anything is installed: standard library only "
                                  "(import anything else inside the function that needs it, in a try)")
                    self.assertNotIn(top, ADDED_AFTER_3_9, "Python 3.9 doesn't have this module, and install.py "
                                     "must load there so that its version check can run")
            if isinstance(node, ast.ImportFrom):
                too_new = NAMES_ADDED_AFTER_3_9.get(node.module, set()) & {alias.name for alias in node.names}
                self.assertEqual(too_new, set(), f"line {node.lineno}: Python 3.9's {node.module} doesn't have these")


def describe_python(python):
    """(its own path, (major, minor)) as a Python command reports them, or None if it can't be run.
    The path is the interpreter itself, not a launcher such as a pyenv shim, which needs a PATH."""
    try:
        out = subprocess.run([python, "-c", "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        executable, version = out.stdout.strip().splitlines()
        major, minor = version.split(".")
        return (executable, (int(major), int(minor))) if out.returncode == 0 and executable else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_python(lowest, below):
    """A Python on this computer with lowest <= version < below, as (path, version), or (None, why not)."""
    candidates = []
    if sys.platform == "darwin":
        # Apple's own Python. Without the command line developer tools, running it opens an
        # install dialog instead; `xcode-select -p` says whether they are there without opening one.
        try:
            tools = subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            tools = False
        if tools:
            candidates.append("/usr/bin/python3")
    for minor in range(lowest[1], below[1]):
        found = shutil.which(f"python{lowest[0]}.{minor}")
        if found:
            candidates.append(found)
    for candidate in candidates:
        described = describe_python(candidate)
        if described is not None and lowest <= described[1] < below:
            return described
    return None, (f"no Python from {lowest[0]}.{lowest[1]} up to (not including) {below[0]}.{below[1]} on this "
                  f"computer (looked at: {', '.join(candidates) or 'nothing'})")


# Loads a copy of install.py and runs main() up to its first question, without asking it.
TO_THE_FIRST_QUESTION = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("install_copy", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
def first_question(current):
    print("REACHED THE FIRST QUESTION")
    raise SystemExit(7)
module.ask_stack = first_question
sys.argv = [sys.argv[1], "--no-browser"]
sys.exit(module.main())
"""

# Builds every message of the version check, for every system, under the Python that runs it.
EVERY_MESSAGE = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("install_copy", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
built = 0
for system, root, sep in (("mac", "/Users/you/dewey-ai", "/"), ("linux", "/home/you/dewey-ai", "/"),
                          ("windows", "C:\\\\Users\\\\you\\\\dewey-ai", "\\\\")):
    for venv, in_dewey, exists, broken in (
            ("", False, False, False), ("", False, True, False), ("", False, False, True),
            (root + sep + ".venv", True, True, False),
            (root + sep + "my env", False, False, False), (root + sep + "my env", False, True, False)):
        for in_root in (True, False):
            where = module.PythonSituation(system=system, root=root, in_root=in_root, venv=venv,
                                           in_dewey_venv=in_dewey, dewey_venv_exists=exists,
                                           dewey_venv_broken=broken)
            for version in ((3, 9, 6), (3, 14, 10), (3, 15, 2), (3, 12, 3)):
                assert module.environment_steps(where, version)
                if module.python_verdict(version) != "ok":
                    assert module.python_check_lines(version, where)
                built += 1
assert module.python_situation().system in ("mac", "linux", "windows")
module.say_lines(module.environment_steps())
print("BUILT", built)
"""


class RealInterpreterTests(unittest.TestCase):
    """The wizard under real Pythons, against a copy of install.py in a temporary folder with an
    empty PATH, no terminal and stdin closed: nothing here can ask, install or write settings."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="dewey-pycheck-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copy2(REPO / "install.py", self.root / "install.py")
        (self.root / "bin").mkdir()

    def run_python(self, python, *args, cwd=None):
        env = {"PATH": str(self.root / "bin"), "HOME": str(self.root), "NO_COLOR": "1",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        for needed_to_start in ("LD_LIBRARY_PATH", "SYSTEMROOT"):  # some Linux builds; Windows
            if os.environ.get(needed_to_start):
                env[needed_to_start] = os.environ[needed_to_start]
        # start_new_session: no controlling terminal, so a hidden prompt could not wait on /dev/tty.
        return subprocess.run([str(python), *args], cwd=str(cwd or self.root), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, encoding="utf-8", errors="replace", timeout=120,
                              start_new_session=True)

    def wizard(self, python, cwd=None):
        return self.run_python(python, "-B", str(self.root / "install.py"), "--no-browser", cwd=cwd)

    def to_the_first_question(self, python):
        return self.run_python(python, "-B", "-c", TO_THE_FIRST_QUESTION, str(self.root / "install.py"))

    def assert_stopped_before_any_question(self, result, word):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"which is {word}.", result.stdout)
        self.assertIn("Dewey needs Python %d.%d to %d.%d; this is " % (install.MIN_PYTHON + install.TESTED_PYTHON),
                      result.stdout)
        for never in ("Which stack?", "Choose a number or name", "Traceback"):
            self.assertNotIn(never, result.stdout + result.stderr)
        self.assertFalse((self.root / ".env").exists())

    def test_this_python_does_what_its_verdict_says(self):
        verdict = install.python_verdict()
        result = self.to_the_first_question(sys.executable)
        if verdict in ("ok", "untested"):
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertIn("REACHED THE FIRST QUESTION", result.stdout)
            self.assertEqual("Dewey is tested with Python" in result.stdout, verdict == "untested")
            self.assertNotIn("Dewey needs Python", result.stdout)
        else:
            self.assert_stopped_before_any_question(result, "too old" if verdict == "old" else "too new")
            self.assertNotIn("REACHED THE FIRST QUESTION", result.stdout)

    def test_an_older_python_on_this_computer_gets_the_instructions(self):
        # Box (a), for real: install.py has to load and run on the old Python to say anything.
        python, found = find_python((3, 9), install.MIN_PYTHON)
        if python is None:
            self.skipTest(found)

        copy = install.PythonSituation(system=install.python_situation().system, root=str(self.root), in_root=False,
                                       venv="", in_dewey_venv=False, dewey_venv_exists=False)
        cd = "\n" + install.environment_steps(copy)[1] + "\n"   # "  cd FOLDER", as this system's shell takes it
        self.assertIn(self.root.name, cd)

        # 1. Outside any environment, none made yet: install a Python, then make one.
        result = self.wizard(python)
        self.assert_stopped_before_any_question(result, "too old")
        self.assertIn("this is %d.%d." % found, result.stdout)
        self.assertRegex(result.stdout, r"\n  (python3\.\d+|py -3\.\d+) -m venv \.venv\n")
        self.assertNotIn("--clear", result.stdout)
        self.assertIn(cd, result.stdout)

        # 2. A .venv made with the old Python (what the README's steps produce on a Mac whose only
        #    python3 is Apple's), not turned on: turn it on, delete nothing.
        made = self.run_python(python, "-m", "venv", "--without-pip", str(self.root / ".venv"))
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        result = self.wizard(python)
        self.assert_stopped_before_any_question(result, "too old")
        self.assertIn("not turned on in this terminal window", result.stdout)
        self.assertIn("\n  source .venv/bin/activate\n" if os.name != "nt" else "Activate.ps1", result.stdout)
        self.assertNotIn("-m venv", result.stdout)
        self.assertNotIn("\n  cd ", result.stdout)   # the terminal is already in the Dewey folder
        # From another folder the commands, which are relative, start with `cd`.
        self.assertIn(cd, self.wizard(python, cwd=self.root / "bin").stdout)

        # 3. Inside that .venv: it has to be made again, and the commands are shown.
        result = self.wizard(self.root / install.venv_python().relative_to(install.ROOT))
        self.assert_stopped_before_any_question(result, "too old")
        self.assertIn("This Python is the one in Dewey's virtual environment (the .venv folder)", result.stdout)
        self.assertRegex(result.stdout, r"\n  (python3\.\d+|py -3\.\d+) -m venv --clear \.venv\n")
        self.assertIn("deletes what is in .venv", result.stdout)

        # 4. Outside it again, after its Python has gone: it is made again, not "turned on".
        (self.root / install.venv_python().relative_to(install.ROOT)).unlink()
        result = self.wizard(python)
        self.assert_stopped_before_any_question(result, "too old")
        self.assertIn("not a working virtual environment", result.stdout)
        self.assertRegex(result.stdout, r"\n  (python3\.\d+|py -3\.\d+) -m venv --clear \.venv\n")
        self.assertNotIn("not turned on", result.stdout)

        # Every message for every system builds under the old Python: nothing in them needs newer.
        result = self.run_python(python, "-B", "-c", EVERY_MESSAGE, str(self.root / "install.py"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BUILT 144", result.stdout)

    @unittest.skipIf(os.name == "nt", "a Windows environment holds a copy of python.exe, not a link to it")
    def test_an_environment_whose_python_is_gone_is_only_repaired_with_clear(self):
        # Why the wizard prints --clear for a .venv that is not a working environment: made again
        # without it, Python's venv keeps the links already in the folder, dead ones included.
        venv, python = self.root / ".venv", self.root / ".venv" / "bin" / "python"

        def make(*options):
            made = self.run_python(sys.executable, "-m", "venv", "--without-pip", *options, str(venv))
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)

        def situation():
            with mock.patch.object(install, "ROOT", self.root), \
                    mock.patch.object(sys, "prefix", getattr(sys, "base_prefix", sys.prefix)):
                return install.python_situation()

        make()
        self.assertTrue(python.exists())
        self.assertEqual((situation().dewey_venv_exists, situation().dewey_venv_broken), (True, False))
        # Uninstalling the Python it was made with leaves its links pointing nowhere.
        links = [path for path in (venv / "bin").iterdir() if path.is_symlink() and "/" in os.readlink(str(path))]
        self.assertTrue(links)
        for link in links:
            link.unlink()
            os.symlink("/nowhere/" + link.name, str(link))
        self.assertFalse(python.exists())
        where = situation()
        self.assertEqual((where.dewey_venv_exists, where.dewey_venv_broken), (False, True))
        self.assertIn("-m venv --clear .venv", "\n".join(install.environment_steps(where)))

        make()
        self.assertFalse(python.exists(), "venv now replaces dead links without --clear: the wizard's wording "
                                          "for a .venv that is not a working environment can be simpler")
        make("--clear")
        self.assertTrue(python.exists())
        self.assertEqual((situation().dewey_venv_exists, situation().dewey_venv_broken), (True, False))

    def test_a_newer_python_on_this_computer_is_refused_before_any_question(self):
        # Box (g), for real.
        python, found = find_python(install.TOO_NEW_PYTHON, (install.TOO_NEW_PYTHON[0], install.TOO_NEW_PYTHON[1] + 6))
        if python is None:
            self.skipTest(found)
        result = self.wizard(python)
        self.assert_stopped_before_any_question(result, "too new")
        self.assertIn("can't be installed on Python %d.%d yet" % found, result.stdout)

    def test_an_untested_python_on_this_computer_is_told_and_carries_on(self):
        between = (install.TESTED_PYTHON[0], install.TESTED_PYTHON[1] + 1)
        python, found = find_python(between, install.TOO_NEW_PYTHON)
        if python is None:
            self.skipTest(found)
        result = self.to_the_first_question(python)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("Dewey is tested with Python", result.stdout)
        self.assertIn("this is %d.%d." % found, result.stdout)
        self.assertLess(result.stdout.index("Dewey is tested with Python"),
                        result.stdout.index("REACHED THE FIRST QUESTION"))
        self.assertNotIn("Dewey needs Python", result.stdout)


if __name__ == "__main__":
    unittest.main()

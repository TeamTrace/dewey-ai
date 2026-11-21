"""
Tests for what the setup wizard says on a first run (issue #19, boxes e and f):

- the stack and search-model lists mark the wizard's own starting point "(suggested)", and call a
  value "(current)" only when it is saved in .env;
- the MCP step says what it did with the token (made one, kept it, replaced it), and that AI
  tools on this computer don't need it, without ever showing the token.

No terminal is needed: input() is patched. Standard library only; no network.
tests/install/test_install.py checks the same things once through a real terminal.
"""
import ast
import contextlib
import importlib.util
import inspect
import io
import pathlib
import sys
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[2]


def load_install():
    spec = importlib.util.spec_from_file_location("dewey_install_first_run", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


install = load_install()

STACKS = [("anthropic", "Claude"), ("open", "open-weight models"), ("azure", "Azure")]


def wizard(function, *args, answers=()):
    """(result, printed text, questions asked) of a wizard function, with `answers` typed in order.
    A question nobody answered is an error, and so is an answer nobody asked for."""
    waiting, asked = list(answers), []

    def answer(prompt=""):
        asked.append(prompt)
        if not waiting:
            raise AssertionError(f"unexpected question: {prompt!r}")
        return waiting.pop(0)

    out = io.StringIO()
    # _STYLED off: install.py decides on colour when it is loaded, and in a developer's terminal
    # the codes would sit between a value and its mark.
    with mock.patch.object(install, "_STYLED", False), contextlib.redirect_stdout(out), \
            mock.patch("builtins.input", answer), mock.patch.object(install.getpass, "getpass", answer):
        result = function(*args)
    if waiting:
        raise AssertionError(f"never asked for: {waiting!r}")
    return result, out.getvalue(), asked


class StackMarkTests(unittest.TestCase):
    """Box (f): "(current)" means saved in .env. A first run has nothing saved."""

    def test_first_run_marks_the_stack_suggested_not_current(self):
        stack, out, asked = wizard(install.ask_stack, {}, answers=[""])
        self.assertEqual(stack, "anthropic")
        self.assertIn("anthropic (suggested):", out)
        self.assertNotIn("(current)", out)
        self.assertEqual(out.count("(suggested)"), 1)
        self.assertEqual(len(asked), 1)
        self.assertIn("[anthropic]", asked[0])   # Enter picks it, as before

    def test_saved_stack_is_marked_current(self):
        for saved in ("open", "Open", " open "):
            with self.subTest(saved=saved):
                stack, out, _ = wizard(install.ask_stack, {"DEWEY_STACK": saved}, answers=[""])
                self.assertEqual(stack, "open")
                self.assertIn("open (current):", out)
                self.assertNotIn("(suggested)", out)
                self.assertEqual(out.count("(current)"), 1)

    def test_another_stack_can_still_be_chosen_by_name_or_number(self):
        self.assertEqual(wizard(install.ask_stack, {}, answers=["azure"])[0], "azure")
        self.assertEqual(wizard(install.ask_stack, {"DEWEY_STACK": "azure"}, answers=["2"])[0], "open")

    def test_first_run_marks_search_models_suggested(self):
        for stack, starting_point in (("anthropic", "voyage"), ("open", "local")):
            with self.subTest(stack=stack):
                chosen, out, asked = wizard(install.ask_retrieval, stack, {}, answers=["", ""])
                self.assertEqual(chosen, {"EMBEDDING_PROVIDER": starting_point, "RERANK_PROVIDER": starting_point})
                self.assertEqual(out.count(f"{starting_point} (suggested):"), 2)   # embeddings, then reranking
                self.assertEqual(out.count("(suggested)"), 2)
                self.assertNotIn("(current)", out)
                self.assertEqual(len(asked), 2)

    def test_saved_search_models_are_marked_current(self):
        saved = {"EMBEDDING_PROVIDER": "local", "RERANK_PROVIDER": "none"}
        chosen, out, _ = wizard(install.ask_retrieval, "anthropic", saved, answers=["", ""])
        self.assertEqual(chosen, saved)
        self.assertIn("local (current):", out)
        self.assertIn("none (current):", out)
        self.assertNotIn("(suggested)", out)
        # One saved, one not: each question is marked on its own.
        chosen, out, _ = wizard(install.ask_retrieval, "open", {"RERANK_PROVIDER": "VOYAGE"}, answers=["", ""])
        self.assertEqual(chosen, {"EMBEDDING_PROVIDER": "local", "RERANK_PROVIDER": "voyage"})
        self.assertIn("local (suggested):", out)
        self.assertIn("voyage (current):", out)

    def test_a_saved_value_that_is_not_an_option_is_not_called_current(self):
        # It used to be offered as the Enter default, refused, and asked again without end.
        for saved in ("antropic", "none", " "):
            with self.subTest(saved=saved):
                stack, out, asked = wizard(install.ask_stack, {"DEWEY_STACK": saved}, answers=[""])
                self.assertEqual(stack, "anthropic")
                self.assertIn("anthropic (suggested):", out)
                self.assertNotIn("(current)", out)
                self.assertEqual(len(asked), 1)
        chosen, out, asked = wizard(install.ask_retrieval, "open", {"EMBEDDING_PROVIDER": "none"}, answers=["", ""])
        self.assertEqual(chosen["EMBEDDING_PROVIDER"], "local")   # "none" is a reranking answer only
        self.assertEqual(len(asked), 2)

    def test_saved_or_suggested(self):
        for cur, expected in (({}, ("anthropic", "suggested")),
                              ({"DEWEY_STACK": ""}, ("anthropic", "suggested")),
                              ({"DEWEY_STACK": "azure"}, ("azure", "current")),
                              ({"DEWEY_STACK": " AZURE\t"}, ("azure", "current")),
                              ({"DEWEY_STACK": "anthropic"}, ("anthropic", "current")),   # saved, and the same
                              ({"DEWEY_STACK": "gcp"}, ("anthropic", "suggested")),
                              ({"OTHER": "open"}, ("anthropic", "suggested"))):
            with self.subTest(cur=cur):
                self.assertEqual(install.saved_or_suggested(cur, "DEWEY_STACK", STACKS, "anthropic"), expected)

    def test_choose_shows_the_mark_it_is_given_and_only_on_the_default(self):
        for mark in ("current", "suggested", "default"):
            with self.subTest(mark=mark):
                value, out, _ = wizard(install.choose, "Which?", STACKS, "open", mark, answers=[""])
                self.assertEqual(value, "open")
                self.assertIn(f"  2. open ({mark}): open-weight models\n", out)
                self.assertIn("  1. anthropic: Claude\n", out)
                self.assertIn("  3. azure: Azure\n", out)

    def test_choose_has_no_default_mark(self):
        # A call that leaves the mark out fails at once, instead of calling its default "current".
        self.assertIs(inspect.signature(install.choose).parameters["mark"].default, inspect.Parameter.empty)
        with self.assertRaises(TypeError), contextlib.redirect_stdout(io.StringIO()):
            install.choose("Which?", STACKS, "open")

    def test_every_choose_call_says_what_its_default_means(self):
        tree = ast.parse((REPO / "install.py").read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "choose"]
        self.assertGreaterEqual(len(calls), 7)   # stack, embeddings, reranking, Postgres, three import questions
        for call in calls:
            with self.subTest(line=call.lineno):
                self.assertFalse(any(isinstance(arg, ast.Starred) for arg in call.args)
                                 or any(keyword.arg is None for keyword in call.keywords),
                                 "write the arguments out, so this test can see the mark")
                self.assertTrue(len(call.args) >= 4 or any(keyword.arg == "mark" for keyword in call.keywords),
                                f"install.py line {call.lineno}: choose() needs a mark: \"current\" for a value "
                                "saved in .env, \"suggested\" for the wizard's own starting point, \"default\" "
                                "for what a step does when nothing is chosen")


class McpTokenMessageTests(unittest.TestCase):
    """Box (e): the MCP step says what it did with the token. The token itself is never shown."""

    SAVED = "saved-TOKEN-0123456789-abcdefghijklmnopq"   # 40 characters

    def test_first_run_says_a_token_was_made_and_stdio_does_not_need_it(self):
        for cur in ({}, {"DEWEY_MCP_TOKEN": ""}):
            with self.subTest(cur=cur):
                values, out, asked = wizard(install.ask_mcp, cur)
                token = values["DEWEY_MCP_TOKEN"]
                self.assertEqual(asked, [])   # nothing to decide: there is no token to keep
                self.assertGreaterEqual(len(token), install.MCP_MIN_TOKEN_CHARS)
                self.assertIn("This run made a new token.", out)
                self.assertIn("will be saved in .env", out)   # not "saved": nothing is written yet
                self.assertIn("DEWEY_MCP_TOKEN", out)
                self.assertRegex(out, r"on this computer \(stdio\) do not need it")
                self.assertIn("docs/MCP.md", out)
                self.assertNotIn(token, out)
                self.assertNotIn("replace", out)

    def test_each_first_run_makes_its_own_token(self):
        self.assertNotEqual(wizard(install.ask_mcp, {})[0], wizard(install.ask_mcp, {})[0])

    def test_keeping_the_token_says_so(self):
        for typed in ("", "y"):
            with self.subTest(typed=typed):
                values, out, asked = wizard(install.ask_mcp, {"DEWEY_MCP_TOKEN": self.SAVED}, answers=[typed])
                self.assertEqual(values, {"DEWEY_MCP_TOKEN": self.SAVED})
                self.assertEqual(len(asked), 1)
                self.assertIn("Keep the existing MCP token?", asked[0])
                self.assertIn("Kept the token", out)
                self.assertNotIn("made a new", out)
                self.assertNotIn(self.SAVED, out)

    def test_replacing_the_token_says_what_to_update(self):
        values, out, asked = wizard(install.ask_mcp, {"DEWEY_MCP_TOKEN": self.SAVED}, answers=["n"])
        token = values["DEWEY_MCP_TOKEN"]
        self.assertNotEqual(token, self.SAVED)
        self.assertGreaterEqual(len(token), install.MCP_MIN_TOKEN_CHARS)
        self.assertEqual(len(asked), 1)
        self.assertIn("This run made a new token to replace it.", out)
        self.assertIn("will be saved in .env", out)
        self.assertIn("restart the MCP server", out)
        self.assertIn("the old one stops working", out)
        self.assertIn("docs/MCP.md", out)
        self.assertNotIn("Kept the token", out)
        for secret in (self.SAVED, token):
            self.assertNotIn(secret, out)

    def test_a_token_too_short_for_the_server_is_replaced_with_the_reason(self):
        short = "tooshort-TOKEN-123"
        values, out, asked = wizard(install.ask_mcp, {"DEWEY_MCP_TOKEN": short})
        token = values["DEWEY_MCP_TOKEN"]
        self.assertEqual(asked, [])   # keeping it is not offered: the server would refuse to start
        self.assertGreaterEqual(len(token), install.MCP_MIN_TOKEN_CHARS)
        self.assertIn(f"shorter than {install.MCP_MIN_TOKEN_CHARS} characters", out)
        self.assertIn("made a new one to replace it", out)
        self.assertIn("will be saved in .env", out)
        self.assertNotIn("the old one stops working", out)   # it never worked
        for secret in (short, token):
            self.assertNotIn(secret, out)

    def test_a_new_token_is_only_promised_for_when_the_answers_are_written(self):
        # This step comes before "Write this to .env?", and with --dry-run nothing is written at
        # all. It used to say flatly that the token "will be saved", then "Afterwards, restart
        # the MCP server ... the old one stops working", on runs that left the old one in place.
        for cur, answers in (({}, []), ({"DEWEY_MCP_TOKEN": self.SAVED}, ["n"]),
                             ({"DEWEY_MCP_TOKEN": "tooshort-TOKEN-123"}, [])):
            with self.subTest(cur=sorted(cur)):
                _, out, _ = wizard(install.ask_mcp, cur, answers=answers)
                told = " ".join(out.split())
                self.assertIn("will be saved in .env with your other answers when you confirm at the end", told)
                self.assertIn("(never with --dry-run)", told)
                self.assertNotIn("Afterwards", out)
                self.assertEqual("Once it is saved, restart the MCP server" in told, bool(cur))
        # A token that is kept makes no promise about saving: nothing about it changes.
        _, out, _ = wizard(install.ask_mcp, {"DEWEY_MCP_TOKEN": self.SAVED}, answers=[""])
        self.assertNotIn("confirm at the end", out)
        self.assertNotIn("Once it is saved", out)

    def test_the_wizard_and_the_server_agree_on_the_shortest_token(self):
        server = (REPO / "app" / "mcp_server.py").read_text(encoding="utf-8")
        self.assertRegex(server, rf"(?m)^MIN_TOKEN_CHARS = {install.MCP_MIN_TOKEN_CHARS}\b")

    def test_the_guide_has_what_the_messages_point_to(self):
        guide = (REPO / "docs" / "MCP.md").read_text(encoding="utf-8")
        security = guide.split("\n## Security\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("**Rotate the token**", security)             # "(docs/MCP.md, under Security)"
        self.assertIn("grep DEWEY_MCP_TOKEN .env", guide)           # "docs/MCP.md shows how to copy it"


class WrittenDownTests(unittest.TestCase):
    """The guides quote the wizard, and box (f) keeps two different stack defaults on purpose:
    both have to stay written down where people look."""

    def read(self, *path):
        return REPO.joinpath(*path).read_text(encoding="utf-8")

    def test_the_two_stack_defaults_differ_and_the_difference_is_written_down(self):
        # With no DEWEY_STACK, Dewey runs azure; the wizard's list starts at anthropic.
        self.assertRegex(self.read("app", "providers", "__init__.py"), r'os\.getenv\("DEWEY_STACK", "azure"\)')
        self.assertEqual(wizard(install.ask_stack, {}, answers=[""])[0], "anthropic")
        template = self.read(".env.template")
        self.assertIn('If this line is missing, Dewey runs "azure"', template)
        self.assertIn('starts at "anthropic", marked "(suggested)"', template)
        self.assertIn('\nDEWEY_STACK="azure"\n', template)
        readme = self.read("README.md")
        self.assertIn('marks `anthropic` "(suggested)"', readme)
        self.assertIn("with no `DEWEY_STACK` line runs `azure`", readme)
        for guide in ("STACKS.md", "GLOSSARY.md", "INSTALL.md"):
            with self.subTest(guide=guide):
                text = self.read("docs", guide)
                self.assertIn('"(suggested)"', text)
                self.assertNotIn('marked "(current)"', text)   # the old first-run wording

    def test_the_install_guide_quotes_what_the_wizard_prints(self):
        guide = self.read("docs", "INSTALL.md")
        _, stack_question, _ = wizard(install.ask_stack, {}, answers=[""])
        (sample,) = [line for line in guide.splitlines() if line.strip().startswith("1. anthropic (")]
        self.assertTrue(sample.rstrip().endswith(" ..."), sample)
        self.assertIn(sample.strip()[:-len("...")], stack_question)   # the guide's line, up to its "..."
        _, mcp_step, _ = wizard(install.ask_mcp, {})
        for quoted in ("This run made a new token.",):
            self.assertIn(f"`{quoted}`", guide)
            self.assertIn(quoted, mcp_step)
        _, _, asked = wizard(install.ask_mcp, {"DEWEY_MCP_TOKEN": "t" * 40}, answers=[""])
        self.assertIn("`Keep the existing MCP token? (Y/n)`", guide)
        self.assertIn("Keep the existing MCP token? (Y/n)", asked[0])


if __name__ == "__main__":
    unittest.main()

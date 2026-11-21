"""
Tests for the wizard's Docker database step (issue #18): which command it prints, which
password it uses and what it saves, for every row of the step's decision table.

    Docker shows              saved password   the wizard
    1 nothing                 none             prints docker run with a new password
    2 nothing                 yes              prints docker run with the saved password
    3 the volume only         none             prints no command (the files keep their password)
    4 the volume only         yes              prints docker run with the saved password
    5 a running container     yes              prints no command and asks nothing
    6 a stopped container     yes              prints docker start
    7 a container             none             makes no password; the address is asked for
    8 no answer               none             prints docker run; the password is kept only after a yes
    9 no answer               yes              prints docker run with the saved password

No Docker, no terminal and no .env are needed: Docker's answer is handed in as a DockerLook
(or as canned text for the two listing commands), and a test fails if the step reaches the
real `docker`, asks a question the test didn't expect, or leaves an answer unused.
"""
import contextlib
import importlib.util
import io
import itertools
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests.docs.test_docs import joined_commands, shell_problems

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO = pathlib.Path(__file__).resolve().parents[2]


def load_install():
    spec = importlib.util.spec_from_file_location("dewey_install_docker", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


install = load_install()

SAVED = "0123456789abcdef01234567"   # a password already in .env, the shape the wizard makes
ELSEWHERE = "postgresql://archive:s3cretPW9@db.example.org:5432/archive"  # some other database
RUN_ONE_LINE = ("docker run -d --name dewey-db --restart unless-stopped -e POSTGRES_USER=dewey "
                "-e POSTGRES_DB=dewey -e POSTGRES_PASSWORD=PW -v dewey-db-data:/var/lib/postgresql "
                "-p 127.0.0.1:5432:5432 pgvector/pgvector:pg18")
START = "docker start dewey-db"
UPDATE = "docker update --restart unless-stopped dewey-db"
EXISTS_HELP = '"The database container already exists" in docs/INSTALL.md'
MISMATCH_HELP = '"The database password doesn\'t match" in docs/INSTALL.md'
OLDER_HELP = '"An older dewey-db" in docs/INSTALL.md'
COMPOSE_LINE = "separate from the db service in docker-compose.yml"
SHOWS_SAVED = "The command below shows the database password saved in .env"  # said before the command
USES_SAVED = "This wizard uses the database password saved in .env"


def url_for(password):
    return f"postgresql://dewey:{password}@127.0.0.1:5432/dewey"


def look(container, volume=False, uses_volume=False):
    return install.DockerLook(container, volume=volume, uses_volume=uses_volume)


class Console:
    """Typed answers in, everything shown out (prompts and visible answers included)."""

    def __init__(self, answers, hidden):
        self.answers, self.hidden, self.out = list(answers), list(hidden), io.StringIO()

    def input(self, prompt=""):
        if not self.answers:
            raise AssertionError(f"unexpected question: {prompt!r}")
        answer = self.answers.pop(0)
        self.out.write(f"{prompt}{answer}\n")
        return answer

    def getpass(self, prompt=""):
        if not self.hidden:
            raise AssertionError(f"unexpected hidden question: {prompt!r}")
        self.out.write(f"{prompt}\n")  # what is typed here stays hidden, as in a terminal
        return self.hidden.pop(0)


@contextlib.contextmanager
def console(answers=(), hidden=(), looks=None, new_password=True):
    """Patch the wizard's input and output. `looks`: what each look at Docker returns, in order
    (None: looking at all fails the test). new_password=False: making one up fails the test."""
    shown = Console(answers, hidden)
    looking = mock.Mock(side_effect=list(looks) if looks is not None
                        else AssertionError("the step looked at the real Docker"))
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("builtins.input", shown.input))
        stack.enter_context(mock.patch.object(install.getpass, "getpass", shown.getpass))
        stack.enter_context(mock.patch.object(install, "_STYLED", False))
        stack.enter_context(mock.patch.object(install, "look_at_docker", looking))
        stack.enter_context(mock.patch.object(install, "docker_answer",
                                              side_effect=AssertionError("the step ran docker")))
        if not new_password:
            stack.enter_context(mock.patch.object(install.secrets, "token_hex",
                                                  side_effect=AssertionError("the step made up a password")))
        stack.enter_context(contextlib.redirect_stdout(shown.out))
        shown.looking = looking
        yield shown
    if shown.answers or shown.hidden:
        raise AssertionError(f"answers nobody asked for: {shown.answers + shown.hidden!r}")


def offer(cur=None, docker=None, answers=(), dry_run=False, new_password=True):
    """Run the Docker step with Docker's answer handed in. Returns (its result, everything shown)."""
    with console(answers, new_password=new_password) as shown:
        result = install.offer_pgvector_container(dict(cur or {}), docker, dry_run=dry_run)
    return result, shown.out.getvalue()


def postgres_question(cur=None, looks=(), answers=(), hidden=(), dry_run=False, new_password=True):
    """Run the whole Postgres question with Docker installed. `looks`: what each look returns."""
    with console(answers, hidden, looks=looks, new_password=new_password) as shown, \
            mock.patch.object(install.shutil, "which", return_value="/usr/local/bin/docker"):
        result = install.ask_postgres(dict(cur or {}), interactive=False, dry_run=dry_run)
    return result, shown.out.getvalue(), shown.looking.call_count


def docker_commands(text):
    """Every command starting with `docker` in what was shown, with continuation lines joined."""
    lines = [line.strip() for line in text.splitlines()]
    return [" ".join(command.split()) for _, command in joined_commands(lines) if command.startswith("docker ")]


def printed_password(text):
    found = re.findall(r"-e POSTGRES_PASSWORD=(\S+) \\", text)
    assert len(found) == 1, f"expected one docker run command, found {len(found)}:\n{text}"
    return found[0]


class TheCommandTests(unittest.TestCase):
    """Rows 1, 2 and 4: Docker shows no container, so the wizard prints docker run."""

    def test_the_printed_command_restarts_by_itself_keeps_its_data_and_matches_the_url(self):
        # The issue's first test. Before the fix the command had no --restart and no -v.
        result, text = offer({}, look("absent"), ["y"])
        self.assertIn("--restart unless-stopped", text)
        self.assertRegex(text, r"-v [\w.-]+:/var/lib/postgresql(?!/)")
        self.assertNotIn("/var/lib/postgresql/data", text)  # Postgres 18 images refuse a new database there
        password = printed_password(text)
        self.assertRegex(password, r"^[0-9a-f]{24}$")
        self.assertEqual(text.count(password), 1, text)  # shown once, inside the command
        self.assertEqual(result, {"DATABASE_URL": url_for(password), "POSTGRES_PASSWORD": password})
        self.assertIn("Is it running? (Y/n)", text)
        # How to start it again, and that Compose's database is another one.
        self.assertIn(f"\n         {START}\n", text)
        self.assertIn("unless you stopped it yourself", text)
        self.assertIn(COMPOSE_LINE, text)
        self.assertIn("An archive indexed in one isn't in the other.", text)
        self.assertNotIn("docker-compose.yml also needs", text)
        self.assertIn("This wizard saves it in .env (as POSTGRES_PASSWORD)", text)

    def test_the_command_is_built_from_the_constants(self):
        self.assertEqual((install.PGVECTOR_CONTAINER, install.PGVECTOR_VOLUME, install.PGVECTOR_MOUNT),
                         ("dewey-db", "dewey-db-data", "/var/lib/postgresql"))  # the wizard's text spells them out
        lines = install.pgvector_run_command("PW")
        self.assertEqual(len(lines), 5)
        self.assertEqual(" ".join(line.rstrip("\\").strip() for line in lines), RUN_ONE_LINE)
        for line in lines[:-1]:
            self.assertTrue(line.endswith(" \\"), line)
        self.assertEqual(install.docker_database_url("PW"), url_for("PW"))

    def test_answering_no_keeps_the_new_password_but_writes_no_address(self):
        # Docker showed that nothing exists, so whenever the command is run it makes a database
        # with exactly this password: keeping it lets the next run print the same command.
        result, text = offer({}, look("absent"), ["n"])
        self.assertEqual(result, {"DATABASE_URL": "", "POSTGRES_PASSWORD": printed_password(text)})
        self.assertIn("start it whenever you like, then re-run this wizard", text)
        self.assertIn("prints the same command", text)
        self.assertIn("Until then Dewey has no database", text)

    def test_a_saved_password_is_reused_and_never_rewritten(self):
        result, text = offer({"POSTGRES_PASSWORD": SAVED}, look("absent"), ["y"], new_password=False)
        self.assertIn(f"-e POSTGRES_PASSWORD={SAVED} \\", text)
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})  # no POSTGRES_PASSWORD: .env's stays as it is
        # The person is told the command shows a saved secret before it appears.
        self.assertIn(f"{SHOWS_SAVED}: the POSTGRES_PASSWORD line.", text)
        self.assertLess(text.index(SHOWS_SAVED), text.index("docker run"))
        self.assertNotIn("was made for you", text)
        # A database this command creates gets exactly this password: nothing to warn about.
        self.assertNotIn(MISMATCH_HELP, text)

    def test_answering_no_with_a_saved_password_changes_nothing(self):
        result, text = offer({"POSTGRES_PASSWORD": SAVED}, look("absent"), ["n"], new_password=False)
        self.assertEqual(result, {"DATABASE_URL": ""})
        self.assertIn("start it whenever you like, then re-run this wizard", text)

    def test_leftover_files_with_a_saved_password_get_the_same_command(self):
        result, text = offer({"POSTGRES_PASSWORD": SAVED}, look("absent", volume=True), ["y"], new_password=False)
        self.assertIn("it still has this database's files", text)
        self.assertIn("volume dewey-db-data", text)
        self.assertEqual(printed_password(text), SAVED)
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})
        self.assertIn("Is it running? (Y/n)", text)
        # The files keep the password they were created with, whatever the command says.
        self.assertIn(MISMATCH_HELP, text)
        # A "no" changes nothing, whatever address was saved.
        for saved_url in ("", ELSEWHERE):
            cur = {"POSTGRES_PASSWORD": SAVED, "DATABASE_URL": saved_url}
            result, _ = offer(cur, look("absent", volume=True), ["n"], new_password=False)
            self.assertEqual(result, {"DATABASE_URL": saved_url})

    def test_an_existing_postgres_password_is_never_replaced(self):
        # One the wizard can't put in a command (punctuation), for example Docker Compose's own.
        cur = {"POSTGRES_PASSWORD": "p@ss-word!"}
        result, text = offer(cur, look("absent"), ["y"])
        password = printed_password(text)
        self.assertRegex(password, r"^[0-9a-f]{24}$")
        self.assertEqual(result, {"DATABASE_URL": url_for(password)})  # no POSTGRES_PASSWORD key
        self.assertNotIn("p@ss-word!", text)
        self.assertIn("saved only inside DATABASE_URL", text)
        self.assertNotIn("This wizard saves it in .env (as POSTGRES_PASSWORD)", text)
        result, text = offer(cur, look("absent"), ["n"])
        self.assertEqual(result, {"DATABASE_URL": ""})
        self.assertIn("isn't saved", text)
        self.assertNotIn("prints the same command", text)

    def test_answering_no_leaves_a_saved_address_alone(self):
        # It used to fall into "later", which dropped a saved DATABASE_URL from .env.
        cur = {"POSTGRES_PASSWORD": SAVED, "DATABASE_URL": url_for(SAVED)}
        result, text = offer(cur, look("exited", volume=True, uses_volume=True), ["n"], new_password=False)
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})
        self.assertIn(".env already has this database's address, and it stays as it is", text)
        cur = {"POSTGRES_PASSWORD": SAVED, "DATABASE_URL": ELSEWHERE}
        result, text = offer(cur, look("absent"), ["n"], new_password=False)
        self.assertEqual(result, {"DATABASE_URL": ELSEWHERE})
        self.assertIn("The address already saved in .env stays as it is.", text)
        self.assertNotIn("s3cretPW9", text)
        result, text, _ = postgres_question(cur, [look("absent")], ["docker", "n"], new_password=False)
        self.assertEqual(result, {"DATABASE_URL": ELSEWHERE})
        self.assertNotIn("Any of these gives Dewey what it needs", text)  # not the "later" list any more

    def test_answering_yes_says_when_a_saved_address_changes(self):
        cur = {"POSTGRES_PASSWORD": SAVED, "DATABASE_URL": ELSEWHERE}
        result, text = offer(cur, look("absent"), ["y"], new_password=False)
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})
        self.assertIn(".env.bak-", text)
        self.assertNotIn("s3cretPW9", text)


class ExistingDatabaseTests(unittest.TestCase):
    """Rows 3, 5, 6 and 7: Docker shows a container or its files, so no new password is right."""

    def test_a_running_container_with_a_saved_password_needs_no_command_and_no_question(self):
        result, text = offer({"POSTGRES_PASSWORD": SAVED}, look("running", volume=True, uses_volume=True),
                             new_password=False)  # no answers queued: any question fails the test
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})
        self.assertNotIn("docker run", text)
        self.assertNotIn(SAVED, text)
        self.assertIn("it is running: nothing to start", text)
        self.assertEqual(docker_commands(text), [START])  # how to start it again, nothing else
        self.assertIn(COMPOSE_LINE, text)
        # The only evidence is POSTGRES_PASSWORD, which Compose's database uses too.
        self.assertIn(f"{USES_SAVED}: the POSTGRES_PASSWORD line.", text)
        self.assertIn(MISMATCH_HELP, text)

    def test_a_password_inside_the_saved_address_needs_no_warning(self):
        result, text = offer({"DATABASE_URL": url_for(SAVED)}, look("running", volume=True, uses_volume=True),
                             new_password=False)
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})  # and it is not copied into POSTGRES_PASSWORD
        self.assertIn("the one inside DATABASE_URL.", text)
        self.assertNotIn(MISMATCH_HELP, text)
        self.assertNotIn(".env.bak-", text)  # the address doesn't change
        self.assertNotIn(SAVED, text)

    def test_a_changed_address_is_announced(self):
        cur = {"POSTGRES_PASSWORD": SAVED, "DATABASE_URL": ELSEWHERE}
        result, text = offer(cur, look("running", volume=True, uses_volume=True), new_password=False)
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})
        self.assertIn("DATABASE_URL held a different address", text)
        self.assertIn(".env.bak-", text)
        self.assertNotIn("s3cretPW9", text)

    def test_a_stopped_container_gets_docker_start_not_docker_run(self):
        cur = {"POSTGRES_PASSWORD": SAVED}
        for state in ("exited", "created", "paused", "restarting", "dead"):
            with self.subTest(state=state):
                result, text = offer(cur, look(state, volume=True, uses_volume=True), ["y"], new_password=False)
                self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})
                self.assertEqual(docker_commands(text), [START])
                self.assertIn(f"\n    {START}\n", text)
                self.assertIn(f'"{state}"', text)  # the state Docker reports, named
                self.assertIn("Is it running? (Y/n)", text)
                self.assertNotIn(SAVED, text)
                result, _ = offer(cur, look(state, volume=True, uses_volume=True), ["n"], new_password=False)
                self.assertEqual(result, {"DATABASE_URL": ""})

    def test_an_existing_container_without_a_saved_password_is_never_given_one(self):
        for cur in ({}, {"POSTGRES_PASSWORD": "p@ss-word!"}, {"DATABASE_URL": ELSEWHERE}):
            for docker in (look("running"), look("exited"), look("running", volume=True, uses_volume=True),
                           look("restarting", volume=True)):
                with self.subTest(cur=sorted(cur), docker=docker):
                    result, text = offer(cur, docker, new_password=False)
                    self.assertIsNone(result)  # the caller asks for the address instead
                    self.assertEqual(docker_commands(text), [])
                    self.assertIn("no saved password", text)
                    self.assertIn(EXISTS_HELP, text)
                    self.assertIn(url_for("PASSWORD"), text)
                    # The wizard tests wait for these two prompts: explanations must not contain them.
                    self.assertNotIn("Database URL", text)
                    self.assertNotIn("Is it running", text)
                    for secret in ("p@ss-word!", "s3cretPW9"):
                        self.assertNotIn(secret, text)
        _, text = offer({}, look("exited"), new_password=False)
        self.assertIn('The container isn\'t running: Docker reports it as "exited".', text)
        _, text = offer({}, look("running"), new_password=False)
        self.assertNotIn("isn't running", text)
        _, text = offer({"DATABASE_URL": ELSEWHERE}, look("running"), new_password=False)
        self.assertIn("the address already saved in .env stays", text)

    def test_the_postgres_question_then_asks_for_the_address(self):
        typed = "postgresql://dewey:typedPW1@127.0.0.1:5432/dewey"
        result, text, looks = postgres_question({}, [look("running")], ["docker"], [typed], new_password=False)
        self.assertEqual(result, {"DATABASE_URL": typed})
        self.assertNotIn("typedPW1", text)
        self.assertEqual(looks, 1)
        self.assertLess(text.index("no saved password"), text.index("Database URL"))
        # Enter keeps what was saved (here: nothing), and still no password is made up.
        result, _, _ = postgres_question({}, [look("exited")], ["docker"], [""], new_password=False)
        self.assertEqual(result, {"DATABASE_URL": ""})
        result, _, _ = postgres_question({"DATABASE_URL": ELSEWHERE}, [look("running")], ["docker"], [""],
                                         new_password=False)
        self.assertEqual(result, {"DATABASE_URL": ELSEWHERE})

    def test_leftover_files_without_a_saved_password_get_no_command(self):
        # The volume outlives the container, and its database keeps the password it was created with.
        result, text = offer({}, look("absent", volume=True), new_password=False)
        self.assertEqual(result, {"DATABASE_URL": ""})
        self.assertEqual(docker_commands(text), [])
        self.assertIn("it still has the volume dewey-db-data", text)
        self.assertIn("no saved password", text)
        self.assertIn(EXISTS_HELP, text)
        self.assertIn("Until then Dewey has no database", text)
        result, text = offer({"DATABASE_URL": ELSEWHERE}, look("absent", volume=True), new_password=False)
        self.assertEqual(result, {"DATABASE_URL": ELSEWHERE})
        self.assertIn("The address already saved in .env stays as it is.", text)

    def test_an_older_container_is_told_how_to_start_with_docker(self):
        # Made by the earlier command (no --restart, no -v) or by hand: it doesn't mount dewey-db-data.
        cur = {"POSTGRES_PASSWORD": SAVED}
        result, text = offer(cur, look("running"), new_password=False)
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})
        self.assertIn("It doesn't use the volume dewey-db-data, so it was made by an earlier version of", text)
        self.assertEqual(docker_commands(text), [UPDATE, START])
        self.assertIn(OLDER_HELP, text)
        self.assertNotIn("unnamed", text)  # the look can't know where its data is
        result, text = offer(cur, look("exited"), ["y"], new_password=False)
        self.assertEqual(docker_commands(text), [START, UPDATE])
        self.assertIn(OLDER_HELP, text)
        _, text = offer(cur, look("running", volume=True, uses_volume=True), new_password=False)
        self.assertNotIn("docker update", text)
        self.assertNotIn("An older dewey-db", text)


class DockerSilentTests(unittest.TestCase):
    """Rows 8 and 9, and the one offer to look again."""

    def test_a_new_password_is_kept_only_after_a_yes(self):
        # The blocker: saved after "n", a password no container has would be trusted by the next run.
        result, text = offer({}, look("unknown"), [""])  # Enter
        password = printed_password(text)
        self.assertEqual(result, {"DATABASE_URL": ""})  # nothing saved, the password least of all
        self.assertIn("Is it running? (y/N)", text)
        self.assertIn("already in use", text)
        self.assertIn("Don't remove anything: answer n", text)
        self.assertIn(EXISTS_HELP, text)
        self.assertIn("only if you answer y below", text)
        self.assertIn("isn't saved", text)
        self.assertNotIn("uses it again if you re-run it", text)
        self.assertNotIn("prints the same command", text)
        self.assertEqual(text.count(password), 1)
        result, text = offer({}, look("unknown"), ["y"])
        password = printed_password(text)
        self.assertEqual(result, {"DATABASE_URL": url_for(password), "POSTGRES_PASSWORD": password})

    def test_a_saved_password_is_still_used(self):
        cur = {"POSTGRES_PASSWORD": SAVED}
        result, text = offer(cur, look("unknown"), ["y"], new_password=False)
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})
        self.assertEqual(printed_password(text), SAVED)
        self.assertIn(SHOWS_SAVED, text)
        self.assertIn("Is it running? (y/N)", text)
        self.assertIn("already in use", text)
        self.assertIn(f"\n    {START}\n", text)  # start it instead
        self.assertNotIn("Don't remove anything", text)
        self.assertIn(MISMATCH_HELP, text)
        result, _ = offer(cur, look("unknown"), [""], new_password=False)
        self.assertEqual(result, {"DATABASE_URL": ""})

    def test_a_password_made_while_docker_was_silent_is_never_trusted_later(self):
        # Run 1: Docker doesn't answer, twice; Enter at "Is it running?" (the answer the wizard advises
        # when the name turns out to be in use).
        first, text, looks = postgres_question({}, [look("unknown"), look("unknown")], ["docker", "", ""])
        self.assertEqual(looks, 2)
        self.assertIn("Docker still didn't answer", text)
        saved = {key: value for key, value in first.items() if value}  # what main() would write
        self.assertEqual(saved, {})
        # Run 2: Docker answers, and shows the database that held the name all along.
        second, text, _ = postgres_question(saved, [look("running")], ["docker"], [""], new_password=False)
        self.assertIn("no saved password", text)
        self.assertIn("Database URL", text)  # the address prompt was reached
        self.assertEqual(second, {"DATABASE_URL": ""})

    def test_docker_is_asked_once_more_when_it_does_not_answer(self):
        # The usual reason is that Docker isn't started yet: after starting it, the look works.
        result, text, looks = postgres_question({}, [look("unknown"), look("absent")], ["docker", "", "y"])
        self.assertEqual(looks, 2)
        self.assertIn("Asking Docker whether a container named dewey-db already exists", text)
        self.assertIn("Docker didn't answer", text)
        self.assertIn("Start Docker, then look again? (Y/n)", text)
        self.assertIn("Is it running? (Y/n)", text)  # row 1 now: Docker showed that nothing exists
        password = printed_password(text)
        self.assertEqual(result, {"DATABASE_URL": url_for(password), "POSTGRES_PASSWORD": password})
        # Declined: one look only, and the careful rows.
        result, text, looks = postgres_question({}, [look("unknown")], ["docker", "n", ""])
        self.assertEqual(looks, 1)
        self.assertIn("Is it running? (y/N)", text)
        self.assertEqual(result, {"DATABASE_URL": ""})
        # An answer the first time: no offer.
        _, text, looks = postgres_question({}, [look("absent")], ["docker", "n"])
        self.assertEqual(looks, 1)
        self.assertNotIn("look again", text)


class SavedPasswordTests(unittest.TestCase):
    def test_which_saved_password_is_used(self):
        pick = install.saved_docker_password
        self.assertEqual(pick({"DATABASE_URL": url_for("fromurl1"), "POSTGRES_PASSWORD": "composepw2"}),
                         ("fromurl1", "DATABASE_URL"))
        # Only the whole address of the wizard's own database counts: anything else is some other
        # database, whose password says nothing about dewey-db's.
        for other in ("postgresql://dewey:fromurl1@localhost:5432/dewey",
                      "postgresql://postgres:fromurl1@127.0.0.1:5432/dewey",
                      "postgresql://dewey:fromurl1@127.0.0.1:5433/dewey",
                      "postgresql://dewey:fromurl1@127.0.0.1:5432/dewey2",
                      "postgresql://dewey:fromurl1@127.0.0.1:5432/dewey?sslmode=disable",
                      "see postgresql://dewey:fromurl1@127.0.0.1:5432/dewey",
                      "postgresql://dewey:from%40url@127.0.0.1:5432/dewey", ELSEWHERE, ""):
            self.assertEqual(pick({"DATABASE_URL": other, "POSTGRES_PASSWORD": "composepw2"}),
                             ("composepw2", "POSTGRES_PASSWORD"), other)
        self.assertEqual(pick({"POSTGRES_PASSWORD": "p@ss-word!"}), ("", ""))
        self.assertEqual(pick({"POSTGRES_PASSWORD": "  "}), ("", ""))
        self.assertEqual(pick({}), ("", ""))

    def test_the_address_wins_and_the_wizard_says_which_it_used(self):
        cur = {"DATABASE_URL": url_for("fromurl1"), "POSTGRES_PASSWORD": "composepw2"}
        result, text = offer(cur, look("running", volume=True, uses_volume=True), new_password=False)
        self.assertEqual(result, {"DATABASE_URL": url_for("fromurl1")})
        self.assertIn("the one inside DATABASE_URL, not the", text)
        self.assertIn("POSTGRES_PASSWORD line, which is different.", text)
        for secret in ("fromurl1", "composepw2"):
            self.assertNotIn(secret, text)

    def test_a_password_from_the_address_is_not_copied_into_postgres_password(self):
        # People type this address shape by hand too (the docs show it), so it is no proof the
        # wizard made the password; and Compose's database reads POSTGRES_PASSWORD.
        for answer in ("y", "n"):
            result, text = offer({"DATABASE_URL": url_for("handtyped9")}, look("absent"), [answer],
                                 new_password=False)
            self.assertEqual(result, {"DATABASE_URL": url_for("handtyped9")})
        self.assertIn(f"{SHOWS_SAVED}: the one inside DATABASE_URL.", text)


class DryRunTests(unittest.TestCase):
    def test_a_dry_run_asks_docker_nothing_shows_no_password_and_asks_nothing(self):
        cur = {"POSTGRES_PASSWORD": SAVED, "DATABASE_URL": url_for(SAVED)}
        # No look is handed in and none may be made; no answers are queued; no password may be made.
        result, text = offer(cur, None, dry_run=True, new_password=False)
        self.assertEqual(result, {"DATABASE_URL": url_for(SAVED)})
        self.assertIn("--dry-run", text)
        self.assertIn("-e POSTGRES_PASSWORD=PASSWORD \\", text)
        self.assertNotIn(SAVED, text)
        self.assertNotIn("Asking Docker", text)
        self.assertEqual(docker_commands(text), [RUN_ONE_LINE.replace("=PW ", "=PASSWORD ")])
        result, text = offer({}, None, dry_run=True, new_password=False)
        self.assertEqual(result, {"DATABASE_URL": ""})

    def test_the_postgres_question_passes_the_dry_run_on(self):
        result, text, looks = postgres_question({}, [], ["docker"], dry_run=True, new_password=False)
        self.assertEqual((result, looks), ({"DATABASE_URL": ""}, 0))
        self.assertIn("--dry-run", text)


class EveryCombinationTests(unittest.TestCase):
    """The rules behind the table, for every mix of Docker's answer, saved settings and typed answer."""

    NEW = "feedfacefeedfacefeedface"  # the password the step makes up, when it makes one
    DOCKER = [look("absent"), look("absent", volume=True), look("running", volume=True, uses_volume=True),
              look("running"), look("exited", volume=True, uses_volume=True), look("created"), look("unknown")]
    SETTINGS = [
        {}, {"POSTGRES_PASSWORD": SAVED}, {"POSTGRES_PASSWORD": "p@ss-word!"},
        {"DATABASE_URL": url_for("fromurl1")}, {"DATABASE_URL": url_for("fromurl1"), "POSTGRES_PASSWORD": SAVED},
        {"DATABASE_URL": ELSEWHERE}, {"DATABASE_URL": ELSEWHERE, "POSTGRES_PASSWORD": SAVED},
        {"DATABASE_URL": ELSEWHERE, "POSTGRES_PASSWORD": "p@ss-word!"},
    ]

    def run_step(self, cur, docker, answer):
        """The step with every question answered the same way. Returns (result, everything shown)."""
        out = io.StringIO()
        with mock.patch("builtins.input", lambda prompt="": answer), \
                mock.patch.object(install, "_STYLED", False), \
                mock.patch.object(install, "look_at_docker", side_effect=AssertionError("looked at the real Docker")), \
                mock.patch.object(install.secrets, "token_hex", return_value=self.NEW), \
                contextlib.redirect_stdout(out):
            result = install.offer_pgvector_container(dict(cur), docker)
        return result, out.getvalue()

    def test_a_new_password_never_meets_a_database_that_may_already_exist(self):
        for docker, cur, answer in itertools.product(self.DOCKER, self.SETTINGS, ("y", "n")):
            with self.subTest(docker=docker, saved=sorted(cur.items()), answer=answer):
                result, text = self.run_step(cur, docker, answer)
                saved, _ = install.saved_docker_password(cur)
                saved_url = cur.get("DATABASE_URL", "")
                seen = docker.container != "unknown"
                exists = docker.container not in ("absent", "unknown")
                if exists or docker.volume or saved:
                    # Docker shows a database or its files, or .env has a password: nothing is made up.
                    self.assertNotIn(self.NEW, text + repr(result))
                if exists and not saved:
                    self.assertIsNone(result)
                    continue
                connected = docker.container == "running" or (answer == "y" and not (docker.volume and not saved))
                self.assertEqual(result["DATABASE_URL"], url_for(saved or self.NEW) if connected else saved_url)
                # POSTGRES_PASSWORD: only a password made in this run, never over one that .env has,
                # and after "Docker didn't answer" only with a yes.
                keeps = (not saved and not exists and not docker.volume and "POSTGRES_PASSWORD" not in cur
                         and (seen or answer == "y"))
                self.assertEqual(result.get("POSTGRES_PASSWORD"), self.NEW if keeps else None)
                self.assertEqual(sorted(result), ["DATABASE_URL"] + ["POSTGRES_PASSWORD"] * keeps)

    def test_nothing_shown_removes_anything_or_gives_a_saved_secret_away(self):
        for docker, cur, answer in itertools.product(self.DOCKER, self.SETTINGS, ("y", "n")):
            with self.subTest(docker=docker, saved=sorted(cur.items()), answer=answer):
                _, text = self.run_step(cur, docker, answer)
                for word in ("docker rm", "docker volume rm", "prune", "docker stop", "docker inspect", "docker exec"):
                    self.assertNotIn(word, text)
                # The saved password in use is shown only inside the docker run command, after a line
                # that says so. Every other saved secret is never shown.
                saved, _ = install.saved_docker_password(cur)
                for secret in ("p@ss-word!", "s3cretPW9", "fromurl1", SAVED):
                    if secret != saved:
                        self.assertNotIn(secret, text)
                if saved and "docker run" in text:
                    self.assertEqual(text.count(saved), 1)
                    self.assertLess(text.index(SHOWS_SAVED), text.index("docker run"))
                elif saved:
                    self.assertNotIn(saved, text)


class PrintedCommandTests(unittest.TestCase):
    """What a person would copy from this step, in any row."""

    ROWS = [
        ({}, look("absent"), ["y"]),
        ({}, look("absent"), ["n"]),
        ({"POSTGRES_PASSWORD": SAVED}, look("absent"), ["y"]),
        ({}, look("absent", volume=True), []),
        ({"POSTGRES_PASSWORD": SAVED}, look("absent", volume=True), ["n"]),
        ({"POSTGRES_PASSWORD": SAVED}, look("running", volume=True, uses_volume=True), []),
        ({"POSTGRES_PASSWORD": SAVED}, look("running"), []),
        ({"DATABASE_URL": url_for(SAVED), "POSTGRES_PASSWORD": "other9"}, look("exited", volume=True,
                                                                               uses_volume=True), ["y"]),
        ({"POSTGRES_PASSWORD": SAVED}, look("paused"), ["n"]),
        ({}, look("running", volume=True, uses_volume=True), []),
        ({"POSTGRES_PASSWORD": "p@ss-word!"}, look("dead"), []),
        ({}, look("unknown"), [""]),
        ({}, look("unknown"), ["y"]),
        ({"POSTGRES_PASSWORD": SAVED}, look("unknown"), ["n"]),
        ({"POSTGRES_PASSWORD": "p@ss-word!"}, look("unknown"), ["y"]),
    ]

    def shown(self):
        for cur, docker, answers in self.ROWS:
            yield offer(cur, docker, answers)[1]
        yield offer({"POSTGRES_PASSWORD": SAVED}, None, dry_run=True)[1]

    def test_every_docker_command_the_step_prints_pastes_safely_into_zsh(self):
        seen = set()
        for text in self.shown():
            for command in docker_commands(text):
                self.assertEqual(shell_problems(command), [], command)
                seen.add(re.sub(r"POSTGRES_PASSWORD=\S+", "POSTGRES_PASSWORD=PW", command))
        # Exactly these. Nothing here removes anything: a command that deletes a database or its
        # files belongs in docs/INSTALL.md, after its warning, not in a block the wizard says to run.
        self.assertEqual(seen, {RUN_ONE_LINE, START, UPDATE})

    def test_no_explanation_can_be_mistaken_for_a_question(self):
        # The pexpect tests in test_install.py wait for these words; so might a person skimming.
        for text in self.shown():
            self.assertLessEqual(text.count("Is it running"), 1, text)
            self.assertNotIn("Database URL", text)
            self.assertNotIn("Choose a number or name", text)

    def test_commands_stand_on_lines_of_their_own(self):
        # Never prose after a command on its line, or a command at the end of a sentence: people
        # copy whole lines.
        for text in self.shown():
            for line in text.splitlines():
                if "docker start dewey-db" in line or "docker update --" in line or "docker run -d" in line:
                    self.assertRegex(line, r"^\s+docker [^.:]*( \\)?$")


class LookAtDockerTests(unittest.TestCase):
    """The two questions the wizard asks Docker, and how it reads the answers."""

    def looked(self, containers, volumes):
        def answer(*args):
            return containers if args[0] == "ps" else volumes
        with mock.patch.object(install, "docker_answer", side_effect=answer) as asked:
            found = install.look_at_docker()
        return found, [call.args[0] for call in asked.call_args_list]

    def test_the_look_reads_names_states_and_volumes_exactly(self):
        volume_id = "5f2b" * 16  # an unnamed volume shows as 64 hex digits
        cases = [
            (("", ""), look("absent")),
            (("dewey-db running dewey-db-data\n", "dewey-db-data\n"), look("running", True, True)),
            (("dewey-db exited dewey-db-data,/host/folder\n", "dewey-db-data\n"), look("exited", True, True)),
            ((f"dewey-db running {volume_id}\n", ""), look("running")),
            # --filter name= matches any name that contains the text: only the exact name counts.
            ((f"dewey-db-old running x\ndewey-db exited {volume_id}\n", ""), look("exited")),
            (("dewey-db-old running dewey-db-data\n", "dewey-db-data-old\n"), look("absent")),
            (("my-dewey-db running\n", "old-dewey-db-data\ndewey-db-data\n"), look("absent", True)),
            (("dewey-db running dewey-db-data-old\n", "dewey-db-data-old\n"), look("running")),
            (("dewey-db,other/db created\n", ""), look("created")),
            (("dewey-db\n", ""), look("not reported")),
            # The state is printed back to the person: only letters, digits, _ and - get through.
            (("dewey-db \x1b[31mexited\x1b[0m\n", ""), look("31mexited0m")),
        ]
        for (containers, volumes), expected in cases:
            with self.subTest(containers=containers, volumes=volumes):
                found, asked = self.looked(containers, volumes)
                self.assertEqual(found, expected)
                self.assertEqual(asked, ["ps", "volume"])

    def test_no_answer_means_unknown_and_the_second_question_is_not_asked(self):
        found, asked = self.looked(None, "dewey-db-data\n")
        self.assertEqual((found, asked), (look("unknown"), ["ps"]))
        found, asked = self.looked("dewey-db running dewey-db-data\n", None)
        self.assertEqual((found, asked), (look("unknown"), ["ps", "volume"]))

    def test_the_look_only_lists_and_gives_up_quietly(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr=None)

        out = io.StringIO()
        with mock.patch.object(install.subprocess, "run", fake_run), contextlib.redirect_stdout(out):
            self.assertEqual(install.look_at_docker(), look("absent"))
        # These two listings and nothing else. Never `docker inspect`: it prints the container's
        # environment, the database password included.
        self.assertEqual([argv for argv, _ in calls], [
            ["docker", "ps", "-a", "--no-trunc", "--filter", "name=dewey-db",
             "--format", "{{.Names}} {{.State}} {{.Mounts}}"],
            ["docker", "volume", "ls", "--filter", "name=dewey-db-data", "--format", "{{.Name}}"],
        ])
        for _, kwargs in calls:
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)   # it must not read the wizard's answers
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)  # only the exit status is trusted
            self.assertEqual(kwargs["timeout"], install.DOCKER_LOOK_TIMEOUT)
            self.assertNotIn("shell", kwargs)

        failures = [FileNotFoundError("docker"), PermissionError("docker"),
                    subprocess.TimeoutExpired("docker", install.DOCKER_LOOK_TIMEOUT)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(install.subprocess, "run", side_effect=failure), \
                        contextlib.redirect_stdout(out):
                    self.assertEqual(install.look_at_docker(), look("unknown"))
        stopped = subprocess.CompletedProcess(["docker"], 1, stdout="dewey-db running x\n", stderr=None)
        with mock.patch.object(install.subprocess, "run", return_value=stopped), contextlib.redirect_stdout(out):
            self.assertEqual(install.look_at_docker(), look("unknown"))  # a non-zero exit: its output is not read
        self.assertEqual(out.getvalue(), "")


class DocsAgreeTests(unittest.TestCase):
    """docs/INSTALL.md and docker-compose.yml say what the wizard does."""

    @classmethod
    def setUpClass(cls):
        cls.guide = (REPO / "docs" / "INSTALL.md").read_text(encoding="utf-8")

    def test_the_install_guide_quotes_the_command_the_wizard_prints(self):
        # The guide gives the command on one line, for PowerShell.
        one_line = " ".join(line.rstrip("\\").strip() for line in install.pgvector_run_command("PASSWORD_FROM_WIZARD"))
        self.assertIn(one_line, self.guide)

    def test_the_guide_has_the_sections_the_wizard_points_to(self):
        shown = "".join([
            offer({}, look("running"))[1],                                # row 7
            offer({}, look("absent", volume=True))[1],                    # row 3
            offer({"POSTGRES_PASSWORD": SAVED}, look("running"))[1],      # an older container
            offer({}, look("unknown"), [""])[1],                          # row 8
        ])
        for title, pointer in (("The database container already exists", EXISTS_HELP),
                               ("The database password doesn't match", MISMATCH_HELP),
                               ("An older dewey-db", OLDER_HELP)):
            with self.subTest(section=title):
                self.assertIn(pointer, shown)
                self.assertIn(f"<summary>{title}</summary>", self.guide)

    def test_the_guide_quotes_the_wizard_as_it_speaks(self):
        _, silent, _ = postgres_question({}, [look("unknown"), look("unknown")], ["docker", "", ""])
        shown = (silent + offer({}, look("running"))[1] + offer({}, look("absent", volume=True))[1]
                 + offer({"POSTGRES_PASSWORD": SAVED}, look("running"))[1])
        for phrase in ("no saved password", "it still has the volume dewey-db-data", "Docker didn't answer",
                       "Start Docker, then look again?", "Is it running? (y/N)",
                       "It doesn't use the volume dewey-db-data"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, shown)
                self.assertIn(f"`{phrase}", self.guide)

    def test_the_readme_says_how_to_start_the_database_again(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"```bash\n{START}\n```", readme)
        self.assertIn("(docs/INSTALL.md#start-dewey-again-later)", readme)
        self.assertIn(f"```bash\n   {START}\n   ```", self.guide)  # and the guide's "Start Dewey again later"

    @unittest.skipIf(yaml is None, "PyYAML is not installed")
    def test_the_wizards_database_is_set_up_like_composes(self):
        db = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["db"]
        command = " ".join(install.pgvector_run_command("PW"))
        self.assertEqual(db["image"], install.PGVECTOR_DOCKER_IMAGE)
        self.assertEqual(db["restart"], "unless-stopped")
        self.assertIn(f"--restart {db['restart']} ", command)
        self.assertEqual([str(volume).split(":", 1)[1] for volume in db["volumes"]], [install.PGVECTOR_MOUNT])
        self.assertIn(f"-v {install.PGVECTOR_VOLUME}:{install.PGVECTOR_MOUNT} ", command)
        for name in ("POSTGRES_USER", "POSTGRES_DB"):
            self.assertIn(f"-e {name}={db['environment'][name]} ", command)


if __name__ == "__main__":
    unittest.main()

"""
Checks on README.md and docs/*.md that a reader would otherwise find the hard way:

- every relative link, and every #anchor (in the same file or another one), points at something real;
- every shell code block can be pasted whole into a stock macOS Terminal. macOS's default shell is
  zsh, and an interactive zsh does not treat '#' as a comment unless INTERACTIVE_COMMENTS is set: a
  pasted `pip install -r requirements.txt  # note` hands '#' and 'note' to pip, and a ';' inside such
  a "comment" runs the rest as a second command. Unquoted [ ] and ? are globs ("no matches found"),
  and '!' triggers history expansion;
- the commands Dewey itself prints for people to copy (the wizard, the importers' --help) follow
  the same rules.

Standard library only, no network.
"""
import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md"))
SHELL_LANGS = {"bash", "sh", "shell", "zsh", "console"}
FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+-]*)")


def blocks_and_prose(text):
    """Split markdown into fenced code blocks (lang, first line number, lines) and prose lines.

    Fences may be indented (inside list items); block lines lose the fence's indentation.
    """
    blocks, prose = [], []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            prose.append((i + 1, lines[i]))
            i += 1
            continue
        indent, fence, lang = len(m.group(1)), m.group(2), m.group(3).lower()
        start, body = i + 1, []
        i += 1
        while i < len(lines) and not re.match(r"^\s*" + re.escape(fence[0]) + "{%d,}\\s*$" % len(fence), lines[i]):
            line = lines[i]
            body.append(line[indent:] if line[:indent].strip() == "" else line.lstrip())
            i += 1
        blocks.append((lang, start + 1, body))
        i += 1  # closing fence
    return blocks, prose


def slugify(heading):
    """GitHub's heading anchors (github-slugger): markup dropped, lowercased, punctuation removed."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)   # links and images keep their text
    text = re.sub(r"<[^>]+>", "", text)                         # inline HTML
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors_of(path):
    _, prose = blocks_and_prose(path.read_text(encoding="utf-8"))
    seen, anchors = {}, set()
    for _, line in prose:
        m = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if m:
            slug = slugify(m.group(1))
            n = seen.get(slug, 0)
            seen[slug] = n + 1
            anchors.add(slug if n == 0 else f"{slug}-{n}")
        for m in re.finditer(r"""<a\s+(?:id|name)=["']([^"']+)["']""", line):
            anchors.add(m.group(1))
    return anchors


def links_in(path):
    """(line number, target) for markdown links outside code; inline code spans are skipped."""
    _, prose = blocks_and_prose(path.read_text(encoding="utf-8"))
    found = []
    for number, line in prose:
        line = re.sub(r"`[^`]*`", "", line)
        for m in re.finditer(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)", line):
            found.append((number, m.group(1)))
        m = re.match(r"^\s*\[[^\]]+\]:\s*<?(\S+?)>?(\s|$)", line)
        if m:
            found.append((number, m.group(1)))
    return found


def scan(line):
    """Characters of a shell line with their quoting: None (bare), "'", '"' or 'esc'."""
    out, quote, i = [], None, 0
    while i < len(line):
        c = line[i]
        if quote is None and c == "\\" and i + 1 < len(line):
            out.append((line[i + 1], "esc"))
            i += 2
            continue
        if quote is None and c in "'\"":
            quote = c
            out.append((c, None))
        elif quote is not None and c == quote:
            quote = None
            out.append((c, None))
        elif quote == '"' and c == "\\" and i + 1 < len(line):
            out.append((line[i + 1], quote))
            i += 2
            continue
        else:
            out.append((c, quote))
        i += 1
    return out


def shell_problems(command):
    """Why a (joined) shell command line is unsafe to paste into interactive zsh, if it is."""
    problems = []
    stripped = command.strip()
    if not stripped:
        return problems
    if re.match(r"^[$%]\s", stripped):
        problems.append("starts with a prompt character; the reader would paste it too")
    chars = scan(command)
    bare = "".join(c if q is None else "_" for c, q in chars)
    for i, (c, q) in enumerate(chars):
        if c == "#" and q is None and (i == 0 or chars[i - 1][0] in " \t"):
            problems.append("has a '#' comment: interactive zsh passes it along as words (put notes in the prose)")
            break
    if re.search(r"<[A-Za-z][^<>]*>", bare):
        problems.append("has a bare <placeholder>: zsh reads < and > as redirections (use CAPITALS)")
    if re.search(r"\S\[", bare):
        problems.append("has unquoted [ ]: zsh treats it as a glob ('no matches found'); quote it")
    if "?" in bare:
        problems.append("has an unquoted '?': zsh treats it as a glob; quote the argument")
    for i, (c, q) in enumerate(chars):
        nxt = chars[i + 1][0] if i + 1 < len(chars) else " "
        if c == "!" and q in (None, '"') and nxt not in " \t=":
            problems.append("has '!' outside single quotes: history expansion")
            break
    if "-m venv" in command and not re.search(r"python3\.\d+ -m venv", command):
        problems.append("creates a venv with an unversioned python: name the version (python3.12 -m venv)")
    if re.search(r"\bdocker[ -]compose\b.*\bconfig\b", command) and not re.search(r"(^|\s)(-q|--quiet)(\s|$)", command):
        problems.append("runs docker compose config without --quiet, which prints .env's secrets")
    return problems


def joined_commands(lines):
    """Shell lines with backslash continuations joined, each with the index of its first line."""
    out, buf, first = [], "", None
    for n, line in enumerate(lines):
        if first is None:
            first = n
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        out.append((first, buf + line))
        buf, first = "", None
    if buf:
        out.append((first, buf))
    return out


class LinksTest(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        cache = {}
        for doc in DOCS:
            for number, target in links_in(doc):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):  # http:, https:, mailto: ...
                    continue
                path_part, _, anchor = target.partition("#")
                dest = (doc.parent / path_part).resolve() if path_part else doc
                with self.subTest(doc=doc.relative_to(ROOT).as_posix(), line=number, target=target):
                    self.assertTrue(dest.exists(), f"{target}: {dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest} doesn't exist")
                    if anchor and dest.suffix == ".md":
                        if dest not in cache:
                            cache[dest] = anchors_of(dest)
                        self.assertIn(anchor, cache[dest], f"{target}: no heading with that anchor in {dest.name}")


class ShellBlocksTest(unittest.TestCase):
    def test_shell_blocks_paste_safely_into_zsh(self):
        for doc in DOCS:
            blocks, _ = blocks_and_prose(doc.read_text(encoding="utf-8"))
            for lang, first_line, lines in blocks:
                if lang not in SHELL_LANGS:
                    continue
                for index, command in joined_commands(lines):
                    for problem in shell_problems(command):
                        with self.subTest(doc=doc.relative_to(ROOT).as_posix(), line=first_line + index):
                            self.fail(f"{command.strip()!r} {problem}")

    def test_the_checker_catches_what_it_should(self):
        bad = [
            "pip install -r requirements.txt  # note",
            "ollama pull qwen3:8b   # or another model; larger models need more RAM",
            "# a whole-line comment",
            "$ python install.py",
            "git clone <your fork>",
            'pip install voyageai[local]',
            "curl https://example.com/?a=b",
            'echo "hi!there"',
            "python -m venv .venv",
            "python3 -m venv .venv",
            "docker compose config",
        ]
        good = [
            "pip install -r requirements.txt",
            'pip install "voyageai[local]"',
            "curl 'https://example.com/?a=b'",
            "python3.12 -m venv .venv",
            "docker compose config --quiet",
            "read -rs GHOST_CONTENT_API_KEY && export GHOST_CONTENT_API_KEY",
            "pg_dump -Fc dewey > ~/dewey.dump",
            "git clone https://github.com/TeamTrace/dewey-ai.git",
            "open http://127.0.0.1:7860/#top",
            "echo 'single quotes keep # and ! and [x] literal'",
            "if [ -f .env ]; then echo yes; fi",
        ]
        for line in bad:
            with self.subTest(bad=line):
                self.assertTrue(shell_problems(line), "should be flagged")
        for line in good:
            with self.subTest(good=line):
                self.assertEqual(shell_problems(line), [])


class PrintedCommandsTest(unittest.TestCase):
    """Commands Dewey prints for people to copy must paste safely too."""

    def test_importer_help_examples(self):
        names = ["wordpress-api", "wordpress-export", "feed", "csv", "drupal", "ghost"]
        for name in names:
            out = subprocess.run([sys.executable, "app/import_archive.py", name, "--help"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            for line in out.stdout.splitlines():
                command = line.strip()
                if not re.match(r"^(python3?|read|export) ", command):
                    continue
                for problem in shell_problems(command.rstrip("\\")):
                    with self.subTest(importer=name, line=command):
                        self.fail(problem)

    def test_wizard_printed_commands(self):
        tree = ast.parse((ROOT / "install.py").read_text(encoding="utf-8"))
        command_like = re.compile(r"(read -rs|export |python|app/\S+\.py|--http|--limit)")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.Call)):
                continue
            strings = []
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "say" and node.args:
                strings.append(node.args[0])
            if isinstance(node, ast.FunctionDef) and node.name == "read_secret_line":
                strings += [n.value for n in ast.walk(node) if isinstance(n, ast.Return) and n.value is not None]
            for value in strings:
                parts = [value.value] if isinstance(value, ast.Constant) and isinstance(value.value, str) else [
                    p.value for p in getattr(value, "values", []) if isinstance(p, ast.Constant) and isinstance(p.value, str)]
                literal = "".join(parts)
                if command_like.search(literal) and re.search(r"\S\s+#\s", literal):
                    with self.subTest(line=node.lineno, text=literal.strip()):
                        self.fail("printed command carries a '#' comment, which zsh would pass along as words")


if __name__ == "__main__":
    unittest.main()

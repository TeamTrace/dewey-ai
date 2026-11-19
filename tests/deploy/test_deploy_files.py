"""Static checks of the deployment files. Docker itself is not needed (or run)."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


MCP_COMMAND = ["python", "app/mcp_server.py", "--http", "--host", "0.0.0.0", "--port", "8765"]
MCP_COMMAND_JSON = "[" + ", ".join(f'"{part}"' for part in MCP_COMMAND) + "]"


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def ignore_patterns(name):
    return [line.strip() for line in read(name).splitlines() if line.strip() and not line.startswith("#")]


def dockerfile_instructions():
    """(INSTRUCTION, arguments) pairs with line continuations joined."""
    text = re.sub(r"\\\n", " ", read("Dockerfile"))
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            word, _, rest = line.partition(" ")
            out.append((word.upper(), rest.strip()))
    return out


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        self.ins = dockerfile_instructions()

    def values(self, word):
        return [rest for w, rest in self.ins if w == word]

    def test_base_image_user_and_command(self):
        self.assertEqual(self.values("FROM"), ["python:3.12-slim"])
        # The image is the archive engine: its default command is the MCP server over HTTP.
        self.assertEqual(self.values("CMD"), [MCP_COMMAND_JSON])
        self.assertEqual(self.values("EXPOSE"), ["8765"])
        users = self.values("USER")
        self.assertTrue(users and users[-1] not in ("root", "0"))
        words = [w for w, _ in self.ins]
        self.assertLess(words.index("USER"), words.index("CMD"))

    def test_environment(self):
        env = " ".join(self.values("ENV"))
        for setting in ["GRADIO_ANALYTICS_ENABLED=False", "PYTHONUNBUFFERED=1"]:
            self.assertIn(setting, env)
        # The demo page is not the image's job: no network address for it baked in.
        self.assertNotIn("GRADIO_SERVER_NAME", env)

    def test_installs(self):
        self.assertIn("INSTALL_LOCAL_MODELS=false", self.values("ARG"))
        run = " ".join(self.values("RUN"))
        self.assertIn("pip install --no-cache-dir -r requirements.txt", run)
        self.assertIn("--index-url https://download.pytorch.org/whl/cpu", run)
        self.assertIn("-r requirements-local.txt", run)
        # The local-models build installs one exact PyTorch version, not whatever is newest.
        self.assertRegex(run, r"pip install --no-cache-dir torch==\d+\.\d+\.\d+ --index-url ")

    def test_copies_only_what_it_needs(self):
        copies = self.values("COPY")
        self.assertEqual(copies, ["requirements.txt requirements-local.txt ./", "app/ ./app/"])

    def test_healthcheck_connects_to_the_mcp_port_on_loopback(self):
        (check,) = self.values("HEALTHCHECK")
        self.assertIn("socket.create_connection(('127.0.0.1', 8765)", check)

    def test_healthcheck_command_is_valid_python(self):
        (check,) = self.values("HEALTHCHECK")
        code = re.search(r'python -c "(.*)"$', check).group(1)
        compile(code, "HEALTHCHECK", "exec")


REQUIREMENT_LINE = re.compile(r"(?P<name>[A-Za-z0-9._-]+)(?:\[(?P<extras>[a-z0-9,_-]+)\])?==(?P<version>[^\s;]+)"
                              r"(?:\s*;\s*(?P<marker>.+))?")


def pinned_requirements():
    """{normalized name: (version, extras, marker or None)} from requirements.txt."""
    pins = {}
    for line in read("requirements.txt").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            m = REQUIREMENT_LINE.fullmatch(line)
            assert m, f"not an exact pin: {line!r}"
            name = re.sub(r"[-_.]+", "-", m["name"]).lower()
            pins[name] = (m["version"], set((m["extras"] or "").split(",")) - {""}, m["marker"])
    return pins


class RequirementsTest(unittest.TestCase):
    """requirements.txt pins every package the install pulls in, so a Docker rebuild can't pick up a
    newly published (possibly malicious) release of a dependency's dependency. Upstream's file was a
    complete freeze; packages added later must bring their dependencies' pins with them."""

    def test_every_line_is_an_exact_pin(self):
        self.assertGreater(len(pinned_requirements()), 50)

    def test_dependencies_of_pinned_packages_are_pinned_too(self):
        from importlib import metadata

        from packaging.markers import default_environment
        from packaging.requirements import Requirement

        pins = pinned_requirements()
        env = default_environment()
        applicable = {n for n, (_, _, marker) in pins.items()
                      if not marker or Requirement(f"x; {marker}").marker.evaluate(env)}
        missing = []
        for name in sorted(applicable):
            version, extras, _ = pins[name]
            try:
                dist = metadata.distribution(name)
            except metadata.PackageNotFoundError:
                self.skipTest(f"requirements.txt is not installed here ({name} is missing)")
            for spec in dist.requires or []:
                req = Requirement(spec)
                if req.marker and not any(req.marker.evaluate({**env, "extra": e}) for e in (extras or {""})):
                    continue
                dep = re.sub(r"[-_.]+", "-", req.name).lower()
                if dep not in pins:
                    missing.append(f"{dep} (needed by {name})")
        self.assertEqual(missing, [], "add these to requirements.txt as exact pins")

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_dependabot_proposes_updates(self):
        config = yaml.safe_load(read(".github/dependabot.yml"))
        ecosystems = {u["package-ecosystem"]: u for u in config["updates"]}
        self.assertEqual(set(ecosystems), {"pip", "docker", "github-actions"})
        for update in ecosystems.values():
            self.assertEqual(update["directory"], "/")


class IgnoreFilesTest(unittest.TestCase):
    def test_dockerignore(self):
        patterns = ignore_patterns(".dockerignore")
        for required in [".git", ".env", ".env.*", "!.env.template", ".venv",
                         "**/__pycache__", "tests", "data/*.json"]:
            self.assertIn(required, patterns)
        # .env.bak-* backups written by install.py hold secrets; .env.* covers them
        self.assertTrue(fnmatch.fnmatch(".env.bak-20261118", ".env.*"))

    def test_gitignore(self):
        patterns = ignore_patterns(".gitignore")
        self.assertIn(".env", patterns)
        self.assertIn(".env.bak-*", patterns)

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs a git checkout")
    def test_git_ignores_secrets(self):
        for path in [".env", ".env.bak-20261118"]:
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
            self.assertEqual(result.returncode, 0, f"{path} is not git-ignored")


@unittest.skipIf(yaml is None, "PyYAML not installed")
class ComposeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("docker-compose.yml")
        cls.compose = yaml.safe_load(cls.text)
        cls.services = cls.compose["services"]

    def test_dewey_service_runs_the_mcp_server(self):
        dewey = self.services["dewey"]
        self.assertEqual(dewey["build"]["context"], ".")
        self.assertEqual(dewey["env_file"], ".env")
        self.assertEqual(dewey["command"], MCP_COMMAND)
        self.assertNotIn("profiles", dewey)  # the engine always runs
        self.assertIn("@db:5432/", dewey["environment"]["DATABASE_URL"])
        self.assertEqual(dewey["depends_on"]["db"]["condition"], "service_healthy")

    def test_mcp_token_is_required(self):
        token = self.services["dewey"]["environment"]["DEWEY_MCP_TOKEN"]
        self.assertRegex(token, r"^\$\{DEWEY_MCP_TOKEN:\?.+\}$")

    def test_mcp_port_is_published_on_loopback_only_and_matches_the_command(self):
        dewey = self.services["dewey"]
        port = dewey["command"][dewey["command"].index("--port") + 1]
        self.assertEqual(port, "8765")
        self.assertEqual(dewey["ports"], [f"127.0.0.1:{port}:{port}"])
        # Inside the container it listens on every interface (so Docker's port mapping and other
        # containers reach it); that also turns off the SDK's Host-header check, see docs/MCP.md.
        self.assertEqual(dewey["command"][dewey["command"].index("--host") + 1], "0.0.0.0")

    def test_mcp_clients_network_reaches_the_mcp_server_only(self):
        self.assertEqual(self.compose["networks"]["mcp-clients"]["name"], "dewey-mcp-clients")
        networks = self.services["dewey"]["networks"]
        self.assertIn("default", networks)  # still reaches db and ollama
        self.assertEqual(networks["mcp-clients"]["aliases"], ["dewey-mcp"])
        for name, service in self.services.items():
            if name != "dewey":
                self.assertNotIn("mcp-clients", service.get("networks", {}), name)

    def test_demo_is_optional_loopback_only_and_pinned(self):
        demo = self.services["demo"]
        self.assertEqual(demo["profiles"], ["demo"])
        self.assertEqual(demo["build"], self.services["dewey"]["build"])
        self.assertEqual(demo["command"], ["python", "app/main.py"])
        env = demo["environment"]
        # .env is also the local-run config; a GRADIO_SERVER_PORT=8000 or GRADIO_SERVER_NAME=127.0.0.1
        # left there would make the container listen where the port mapping doesn't reach.
        # `environment` overrides `env_file` in Compose, so pin both there.
        self.assertEqual(env["GRADIO_SERVER_NAME"], "0.0.0.0")
        self.assertEqual(env["GRADIO_SERVER_PORT"], "7860")
        self.assertEqual(env["DEWEY_DEMO_PUBLISHED_ON_LOOPBACK"], "true")
        self.assertNotIn("DEWEY_DEMO_BEHIND_AUTH_PROXY", env)
        self.assertEqual(demo["ports"], [f"127.0.0.1:7860:{env['GRADIO_SERVER_PORT']}"])
        self.assertIn("@db:5432/", env["DATABASE_URL"])
        check = " ".join(demo["healthcheck"]["test"])
        self.assertIn("http://127.0.0.1:7860/", check)
        compile(demo["healthcheck"]["test"][-1], "healthcheck", "exec")

    def test_demo_plan_accepts_the_compose_settings(self):
        import main

        env = {k: str(v) for k, v in self.services["demo"]["environment"].items() if "$" not in str(v)}
        plan = main.build_launch_plan(env)
        self.assertEqual(plan.launch_kwargs["server_name"], "0.0.0.0")
        self.assertEqual(plan.launch_kwargs["server_port"], 7860)
        (middleware,) = plan.launch_kwargs["app_kwargs"]["middleware"]
        self.assertIs(middleware.kwargs["loopback_only"], True)

    def test_no_login_system_left(self):
        for word in ["users.txt", "DEWEY_USERS", "manage_users", "DEWEY_ALLOW_NO_AUTH"]:
            self.assertNotIn(word, self.text)
        # No bind mounts of host files (a users file, .env, ...): every volume is a named volume
        # declared at the top level. Short syntax is "source:target[:mode]"; a bind mount's source
        # is a path such as ./users.txt or /etc/dewey.
        declared = set(self.compose.get("volumes") or {})
        for name, service in self.services.items():
            for volume in service.get("volumes", []):
                with self.subTest(service=name, volume=volume):
                    if isinstance(volume, dict):
                        self.assertEqual(volume.get("type"), "volume")
                        source = volume.get("source")
                    else:
                        source = str(volume).split(":", 1)[0]
                    self.assertIn(source, declared)

    def test_db_service(self):
        db = self.services["db"]
        self.assertEqual(db["image"], "pgvector/pgvector:pg18")
        self.assertNotIn("ports", db)  # never published to the host
        self.assertNotIn("networks", db)  # default network only, not dewey-mcp-clients
        self.assertRegex(db["environment"]["POSTGRES_PASSWORD"], r"^\$\{POSTGRES_PASSWORD:\?.+\}$")
        self.assertIn("pg_isready", " ".join(db["healthcheck"]["test"]))
        # Postgres 18+ images store data under /var/lib/postgresql (a mount on .../data fails)
        self.assertIn("pgdata:/var/lib/postgresql", db["volumes"])
        self.assertIn("pgdata", self.compose["volumes"])

    def test_password_is_required_everywhere_it_is_used(self):
        uses = re.findall(r"\$\{POSTGRES_PASSWORD([^}]*)\}", self.text)
        self.assertTrue(uses)
        for modifier in uses:
            self.assertTrue(modifier.startswith(":?"), modifier)

    def test_ollama_is_optional(self):
        ollama = self.services["ollama"]
        self.assertEqual(ollama["profiles"], ["ollama"])
        self.assertTrue(ollama["image"].startswith("ollama/ollama"))
        self.assertIn("ollama:/root/.ollama", ollama["volumes"])
        self.assertNotIn("ports", ollama)
        self.assertNotIn("ollama", self.services["dewey"].get("depends_on", {}))

    def test_ports_are_published_only_on_loopback(self):
        published = []
        for name, service in self.services.items():
            for mapping in service.get("ports", []):
                self.assertTrue(str(mapping).startswith("127.0.0.1:"), f"{name}: {mapping}")
                published.append(name)
        self.assertEqual(sorted(published), ["demo", "dewey"])

    def test_header_documents_the_indexer(self):
        self.assertIn("docker compose run --rm -v ./data:/app/data:ro dewey python app/setup_postgres.py", self.text)
        self.assertTrue((ROOT / "app" / "setup_postgres.py").exists())


def docker_compose_available():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(docker_compose_available(), "needs the docker CLI with the compose plugin (no daemon)")
class ComposeConfigTest(unittest.TestCase):
    """`docker compose config` parses and interpolates the file (no daemon needed), against a
    throwaway project directory with a fake .env, so a developer's real .env is never read."""

    def config(self, dotenv, *args):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / ".env").write_text(dotenv)
            # DOCKER_CONFIG is redirected to tmp so the developer's real config is never
            # read, but that also hides the compose plugin when it lives outside docker's
            # default search path: Homebrew installs it in /opt/homebrew/lib/docker/
            # cli-plugins and registers it only through cliPluginsExtraDirs. Carry that
            # setting over, or `docker compose` is not a command and -f reaches docker.
            real_config = pathlib.Path(
                os.environ.get("DOCKER_CONFIG") or pathlib.Path.home() / ".docker") / "config.json"
            try:
                extra_dirs = json.loads(real_config.read_text()).get("cliPluginsExtraDirs") or []
            except (OSError, ValueError):
                extra_dirs = []
            if extra_dirs:
                (pathlib.Path(tmp) / "config.json").write_text(
                    json.dumps({"cliPluginsExtraDirs": extra_dirs}))
            env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "DOCKER_CONFIG": tmp}
            return subprocess.run(
                ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "--project-directory", tmp,
                 *args, "config"],
                capture_output=True, text=True, env=env, cwd=tmp, timeout=60)

    GOOD = "POSTGRES_PASSWORD='abc123'\nDEWEY_MCP_TOKEN='" + "t" * 43 + "'\n"

    def test_valid_with_all_profiles(self):
        result = self.config(self.GOOD, "--profile", "demo", "--profile", "ollama")
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = yaml.safe_load(result.stdout) if yaml else None
        if rendered:
            self.assertEqual(set(rendered["services"]), {"dewey", "demo", "db", "ollama"})

    def test_default_starts_only_the_engine(self):
        result = self.config(self.GOOD)
        self.assertEqual(result.returncode, 0, result.stderr)
        if yaml:
            self.assertEqual(set(yaml.safe_load(result.stdout)["services"]), {"dewey", "db"})

    def test_missing_token_or_password_stops_compose(self):
        for dotenv, name in [("POSTGRES_PASSWORD='abc123'\n", "DEWEY_MCP_TOKEN"),
                             ("DEWEY_MCP_TOKEN='" + "t" * 43 + "'\n", "POSTGRES_PASSWORD")]:
            with self.subTest(missing=name):
                result = self.config(dotenv)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class FlyExampleTest(unittest.TestCase):
    def test_parses_and_matches_the_image(self):
        config = tomllib.loads(read("deploy/fly.toml.example"))
        http = config["http_service"]
        self.assertEqual(http["internal_port"], 8765)  # the Dockerfile's MCP command
        self.assertIs(http["force_https"], True)
        self.assertIn(http["auto_stop_machines"], ("off", "stop", "suspend"))
        self.assertIsInstance(http["auto_start_machines"], bool)
        self.assertIsInstance(http["min_machines_running"], int)
        self.assertNotIn("checks", http)  # an HTTP check would get 401 from /mcp
        (check,) = config["checks"].values()
        self.assertEqual(check["type"], "tcp")
        self.assertEqual(check["port"], 8765)
        self.assertEqual(config["build"]["args"]["INSTALL_LOCAL_MODELS"], "false")
        self.assertNotIn("experimental", config)  # runs the image's own command
        for value in config["env"].values():
            self.assertIsInstance(value, str)  # Fly env values must be strings

    def test_no_secrets_in_env(self):
        config = tomllib.loads(read("deploy/fly.toml.example"))
        for key in config["env"]:
            self.assertNotRegex(key, r"KEY|PASSWORD|SECRET|TOKEN|DATABASE_URL")


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RenderExampleTest(unittest.TestCase):
    def test_parses_and_declares_secrets_without_values(self):
        blueprint = yaml.safe_load(read("deploy/render.yaml.example"))
        self.assertEqual(set(blueprint), {"services"})
        (web,) = blueprint["services"]
        self.assertEqual(web["type"], "web")
        self.assertEqual(web["runtime"], "docker")
        self.assertNotIn("healthCheckPath", web)  # /mcp answers 401; Render falls back to a TCP check
        env = {item["key"]: item for item in web["envVars"]}
        for secret in ["DEWEY_MCP_TOKEN", "DATABASE_URL", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY"]:
            self.assertIs(env[secret].get("sync"), False, secret)
            self.assertNotIn("value", env[secret], secret)
        for item in web["envVars"]:
            if "value" in item:
                self.assertIsInstance(item["value"], str, item["key"])

    def test_command_runs_the_mcp_server_on_renders_port(self):
        web = yaml.safe_load(read("deploy/render.yaml.example"))["services"][0]
        command = web["dockerCommand"].split()
        self.assertEqual(command[:3], ["python", "app/mcp_server.py", "--http"])
        self.assertEqual(command[command.index("--host") + 1], "0.0.0.0")
        self.assertRegex(command[command.index("--port") + 1], r"^\d+$")


class DocsTest(unittest.TestCase):
    def test_readme_and_claude_md_point_to_the_guides(self):
        for name in ["README.md", "CLAUDE.md"]:
            self.assertIn("docs/DEPLOYMENT.md", read(name))
            self.assertIn("docs/MCP.md", read(name))

    def test_guides_internal_links_resolve(self):
        for name in ["docs/DEPLOYMENT.md", "docs/MCP.md"]:
            text = read(name)
            anchors = set()
            for heading in re.findall(r"^#{1,6} (.+)$", text, re.M):
                slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
                anchors.add(slug)
            for target in re.findall(r"\]\(#([^)]+)\)", text):
                with self.subTest(doc=name, anchor=target):
                    self.assertIn(target, anchors)

    def test_settings_documented(self):
        text = read("docs/DEPLOYMENT.md")
        for name in ["GRADIO_SERVER_NAME", "GRADIO_SERVER_PORT", "PORT", "DEWEY_DEMO_BEHIND_AUTH_PROXY",
                     "DEWEY_DEMO_PUBLISHED_ON_LOOPBACK", "DEWEY_CONCURRENCY", "POSTGRES_PASSWORD",
                     "DEWEY_MCP_TOKEN", "INSTALL_LOCAL_MODELS"]:
            self.assertTrue(f"`{name}`" in text, f"docs/DEPLOYMENT.md doesn't document {name}")

    def test_compose_config_commands_dont_print_secrets(self):
        # `docker compose config` prints the whole file with .env interpolated: API keys,
        # POSTGRES_PASSWORD and DEWEY_MCP_TOKEN end up in the terminal (or a CI log). Commands the
        # docs tell people to run must validate quietly.
        names = ["README.md", "CLAUDE.md"] + [str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("*.md")]
        for name in names:
            for block in re.findall(r"^```[^\n]*\n(.*?)^```", read(name), re.M | re.S):
                for line in block.splitlines():
                    command = line.split("#", 1)[0]
                    if re.search(r"\bdocker[ -]compose\b.*\bconfig\b", command):
                        with self.subTest(file=name, line=line.strip()):
                            self.assertRegex(command, r"(^|\s)(-q|--quiet)(\s|$)")

    def test_deployment_guide_warns_about_docker_before_28(self):
        # Ports published on 127.0.0.1 are what keep the demo (no login) and the MCP server (plain
        # HTTP) off the network; Docker's own docs say that fails before Docker Engine 28.0.
        text = read("docs/DEPLOYMENT.md")
        self.assertIn("28.0", text)
        self.assertIn("github.com/docker/docs/blob/main/content/manuals/engine/network/port-publishing.md", text)

    def test_no_login_system_left(self):
        # Dewey has no login system: access control is the MCP token, TLS and network placement.
        files = ["README.md", "CLAUDE.md", "docs/DEPLOYMENT.md", "docs/MCP.md", "docs/MODELS.md",
                 "docker-compose.yml", "Dockerfile", ".env.template", ".dockerignore", ".gitignore",
                 "deploy/fly.toml.example", "deploy/render.yaml.example"]
        files += [str(p.relative_to(ROOT)) for p in (ROOT / "app").glob("*.py")]
        for name in files:
            text = read(name)
            for word in ["users.txt", "DEWEY_USERS", "manage_users", "DEWEY_ALLOW_NO_AUTH", "DEWEY_AUTH_MESSAGE"]:
                with self.subTest(file=name, word=word):
                    self.assertFalse(word in text, f"{name} still mentions {word}")
        self.assertFalse((ROOT / "app" / "auth.py").exists())
        self.assertFalse((ROOT / "app" / "manage_users.py").exists())


if __name__ == "__main__":
    unittest.main()

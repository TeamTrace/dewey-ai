"""Static checks of the deployment files. Docker itself is not needed (or run)."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))

import fnmatch
import re
import shutil
import subprocess
import unittest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


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
        self.assertEqual(self.values("CMD"), ['["python", "app/main.py"]'])
        self.assertEqual(self.values("EXPOSE"), ["7860"])
        users = self.values("USER")
        self.assertTrue(users and users[-1] not in ("root", "0"))
        words = [w for w, _ in self.ins]
        self.assertLess(words.index("USER"), words.index("CMD"))

    def test_environment(self):
        env = " ".join(self.values("ENV"))
        for setting in ["GRADIO_SERVER_NAME=0.0.0.0", "GRADIO_SERVER_PORT=7860",
                        "GRADIO_ANALYTICS_ENABLED=False", "PYTHONUNBUFFERED=1"]:
            self.assertIn(setting, env)

    def test_installs(self):
        self.assertIn("INSTALL_LOCAL_MODELS=false", self.values("ARG"))
        run = " ".join(self.values("RUN"))
        self.assertIn("pip install --no-cache-dir -r requirements.txt", run)
        self.assertIn("--index-url https://download.pytorch.org/whl/cpu", run)
        self.assertIn("-r requirements-local.txt", run)

    def test_copies_only_what_it_needs(self):
        copies = self.values("COPY")
        self.assertEqual(copies, ["requirements.txt requirements-local.txt ./", "app/ ./app/"])

    def test_healthcheck_uses_urllib_on_loopback(self):
        (check,) = self.values("HEALTHCHECK")
        self.assertIn("urllib.request.urlopen", check)
        self.assertIn("http://127.0.0.1:", check)
        self.assertIn("7860", check)

    def test_healthcheck_command_is_valid_python(self):
        (check,) = self.values("HEALTHCHECK")
        code = re.search(r'python -c "(.*)"$', check).group(1)
        compile(code, "HEALTHCHECK", "exec")


class IgnoreFilesTest(unittest.TestCase):
    def test_dockerignore(self):
        patterns = ignore_patterns(".dockerignore")
        for required in [".git", ".env", ".env.*", "!.env.template", "users.txt", ".venv",
                         "**/__pycache__", "tests", "data/*.json"]:
            self.assertIn(required, patterns)
        # .env.bak-* backups written by install.py hold secrets; .env.* covers them
        self.assertTrue(fnmatch.fnmatch(".env.bak-20261118", ".env.*"))

    def test_gitignore(self):
        patterns = ignore_patterns(".gitignore")
        self.assertIn(".env", patterns)
        self.assertIn("users.txt", patterns)

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs a git checkout")
    def test_git_ignores_secrets(self):
        for path in ["users.txt", ".env", "deploy/users.txt"]:
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
            self.assertEqual(result.returncode, 0, f"{path} is not git-ignored")


@unittest.skipIf(yaml is None, "PyYAML not installed")
class ComposeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("docker-compose.yml")
        cls.compose = yaml.safe_load(cls.text)
        cls.services = cls.compose["services"]

    def test_dewey_service(self):
        dewey = self.services["dewey"]
        self.assertEqual(dewey["build"]["context"], ".")
        self.assertEqual(dewey["env_file"], ".env")
        self.assertEqual(dewey["ports"], ["127.0.0.1:7860:7860"])
        self.assertIn("@db:5432/", dewey["environment"]["DATABASE_URL"])
        self.assertEqual(dewey["depends_on"]["db"]["condition"], "service_healthy")

    def test_listen_address_and_port_cannot_come_from_dotenv(self):
        # .env is also the local-run config; a GRADIO_SERVER_PORT=8000 or
        # GRADIO_SERVER_NAME=127.0.0.1 left there would make the container listen
        # where the "127.0.0.1:7860:7860" mapping doesn't reach, while the
        # HEALTHCHECK (which reads the same variable) still reports healthy.
        # `environment` overrides `env_file` in Compose, so pin both there.
        env = self.services["dewey"]["environment"]
        self.assertEqual(env.get("GRADIO_SERVER_NAME"), "0.0.0.0")
        self.assertEqual(env.get("GRADIO_SERVER_PORT"), "7860")
        self.assertEqual(self.services["dewey"]["ports"], [f"127.0.0.1:7860:{env['GRADIO_SERVER_PORT']}"])

    def test_users_file_is_mounted_read_only_and_never_auto_created(self):
        mounts = [v for v in self.services["dewey"]["volumes"] if isinstance(v, dict)]
        (users,) = [m for m in mounts if m["target"] == "/app/users.txt"]
        self.assertEqual(users["type"], "bind")
        self.assertIs(users["read_only"], True)
        self.assertIs(users["bind"]["create_host_path"], False)
        self.assertEqual(self.services["dewey"]["environment"]["DEWEY_USERS_FILE"], "/app/users.txt")

    def test_db_service(self):
        db = self.services["db"]
        self.assertEqual(db["image"], "pgvector/pgvector:pg18")
        self.assertNotIn("ports", db)  # never published to the host
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

    def test_only_dewey_publishes_a_port_and_only_on_loopback(self):
        for name, service in self.services.items():
            for mapping in service.get("ports", []):
                self.assertTrue(str(mapping).startswith("127.0.0.1:"), f"{name}: {mapping}")


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class FlyExampleTest(unittest.TestCase):
    def test_parses_and_matches_the_image(self):
        config = tomllib.loads(read("deploy/fly.toml.example"))
        http = config["http_service"]
        self.assertEqual(http["internal_port"], 7860)  # Dockerfile's GRADIO_SERVER_PORT
        self.assertIs(http["force_https"], True)
        self.assertIn(http["auto_stop_machines"], ("off", "stop", "suspend"))
        self.assertIsInstance(http["auto_start_machines"], bool)
        self.assertIsInstance(http["min_machines_running"], int)
        (check,) = config["http_service"]["checks"]
        self.assertEqual(check["path"], "/")
        self.assertEqual(config["build"]["args"]["INSTALL_LOCAL_MODELS"], "false")
        for value in config["env"].values():
            self.assertIsInstance(value, str)  # Fly env values must be strings

    def test_no_secrets_in_env(self):
        config = tomllib.loads(read("deploy/fly.toml.example"))
        for key in config["env"]:
            self.assertNotRegex(key, r"KEY|PASSWORD|SECRET|TOKEN|DATABASE_URL|DEWEY_USERS")


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RenderExampleTest(unittest.TestCase):
    def test_parses_and_declares_secrets_without_values(self):
        blueprint = yaml.safe_load(read("deploy/render.yaml.example"))
        self.assertEqual(set(blueprint), {"services"})
        (web,) = blueprint["services"]
        self.assertEqual(web["type"], "web")
        self.assertEqual(web["runtime"], "docker")
        self.assertEqual(web["healthCheckPath"], "/")
        env = {item["key"]: item for item in web["envVars"]}
        for secret in ["DATABASE_URL", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "DEWEY_USERS"]:
            self.assertIs(env[secret].get("sync"), False, secret)
            self.assertNotIn("value", env[secret], secret)
        self.assertRegex(env["GRADIO_SERVER_PORT"]["value"], r"^\d+$")
        for item in web["envVars"]:
            if "value" in item:
                self.assertIsInstance(item["value"], str, item["key"])


class DocsTest(unittest.TestCase):
    def test_readme_and_claude_md_point_to_the_deployment_guide(self):
        self.assertIn("docs/DEPLOYMENT.md", read("README.md"))
        self.assertIn("docs/DEPLOYMENT.md", read("CLAUDE.md"))

    def test_deployment_guide_internal_links_resolve(self):
        text = read("docs/DEPLOYMENT.md")
        anchors = set()
        for heading in re.findall(r"^#{1,6} (.+)$", text, re.M):
            slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
            anchors.add(slug)
        for target in re.findall(r"\]\(#([^)]+)\)", text):
            self.assertIn(target, anchors)

    def test_settings_documented(self):
        text = read("docs/DEPLOYMENT.md")
        for name in ["GRADIO_SERVER_NAME", "GRADIO_SERVER_PORT", "PORT", "DEWEY_USERS_FILE", "DEWEY_USERS",
                     "DEWEY_ALLOW_NO_AUTH", "DEWEY_AUTH_MESSAGE", "DEWEY_CONCURRENCY", "POSTGRES_PASSWORD"]:
            self.assertIn(f"`{name}`", text)


if __name__ == "__main__":
    unittest.main()

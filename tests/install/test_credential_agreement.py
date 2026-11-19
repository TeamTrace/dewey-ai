"""install.py and app/providers must agree on what counts as a Claude credential.

The installer is standard library only and runs before the dependencies exist, so it
can't import providers.anthropic_credential_source() and carries its own copy. This
test is what stops the two drifting apart: if one learns about a new credential source
and the other doesn't, the wizard will either demand a key Dewey doesn't need or
promise one it does.
"""
import pathlib
import sys

import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "app"))

try:
    import providers  # noqa: E402
except ImportError:  # the installer's own tests run before requirements.txt is installed
    providers = None


def load_install():
    spec = importlib.util.spec_from_file_location("dewey_install_cred", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


install = load_install()

FEDERATION = {
    "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_01",
    "ANTHROPIC_ORGANIZATION_ID": "00000000-0000-0000-0000-000000000000",
    "ANTHROPIC_IDENTITY_TOKEN_FILE": "/var/run/secrets/anthropic.com/token",
}


@unittest.skipIf(providers is None, "needs requirements.txt (providers imports pydantic)")
class AgreementTest(unittest.TestCase):

    def config_dir(self, profiles=(), active=None):
        root = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "configs").mkdir()
        for name in profiles:
            (root / "configs" / f"{name}.json").write_text(json.dumps({"version": "1.0"}))
        if active is not None:
            (root / "active_config").write_text(active + "\n")
        return str(root)

    def assertAgree(self, env, expected, why):
        """Both implementations must reach the same verdict for the same environment."""
        with mock.patch.dict(os.environ, env, clear=True):
            app_side = bool(providers.anthropic_credential_source())
            # The installer reads the key from the values it is about to write, not os.environ.
            values = {"ANTHROPIC_API_KEY": env.get("ANTHROPIC_API_KEY", "")}
            installer_side = install.anthropic_credential_present(values)
        self.assertEqual(app_side, expected, f"providers disagreed: {why}")
        self.assertEqual(installer_side, expected, f"installer disagreed: {why}")

    def test_nothing_at_all(self):
        self.assertAgree({"ANTHROPIC_CONFIG_DIR": self.config_dir()}, False, "no credential")

    def test_an_api_key(self):
        self.assertAgree({"ANTHROPIC_CONFIG_DIR": self.config_dir(), "ANTHROPIC_API_KEY": "sk-ant-x"},
                         True, "api key")

    def test_an_auth_token(self):
        self.assertAgree({"ANTHROPIC_CONFIG_DIR": self.config_dir(), "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x"},
                         True, "auth token")

    def test_federation(self):
        self.assertAgree({**FEDERATION, "ANTHROPIC_CONFIG_DIR": self.config_dir()},
                         True, "federation")

    def test_partial_federation_is_not_a_credential(self):
        partial = {k: v for k, v in FEDERATION.items() if k != "ANTHROPIC_FEDERATION_RULE_ID"}
        self.assertAgree({**partial, "ANTHROPIC_CONFIG_DIR": self.config_dir()},
                         False, "federation missing the rule id")

    def test_the_default_profile(self):
        self.assertAgree({"ANTHROPIC_CONFIG_DIR": self.config_dir(profiles=["default"])},
                         True, "default profile")

    def test_the_active_profile(self):
        self.assertAgree({"ANTHROPIC_CONFIG_DIR": self.config_dir(profiles=["work"], active="work")},
                         True, "active_config profile")

    def test_a_named_profile(self):
        env = {"ANTHROPIC_CONFIG_DIR": self.config_dir(profiles=["staging"]), "ANTHROPIC_PROFILE": "staging"}
        self.assertAgree(env, True, "ANTHROPIC_PROFILE")

    def test_a_named_profile_that_is_missing(self):
        env = {"ANTHROPIC_CONFIG_DIR": self.config_dir(profiles=["default"]), "ANTHROPIC_PROFILE": "gone"}
        self.assertAgree(env, False, "ANTHROPIC_PROFILE naming a missing profile")


class MissingRequiredTest(unittest.TestCase):

    def test_a_profile_means_the_key_is_not_reported_missing(self):
        root = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "configs").mkdir()
        (root / "configs" / "default.json").write_text("{}")
        with mock.patch.dict(os.environ, {"ANTHROPIC_CONFIG_DIR": str(root)}, clear=True):
            missing = install.missing_required({"DEWEY_STACK": "anthropic", "DATABASE_URL": "postgresql://x/y"})
        self.assertEqual(missing, [])

    def test_without_any_credential_the_key_is_still_reported(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_CONFIG_DIR": self.empty()}, clear=True):
            missing = install.missing_required({"DEWEY_STACK": "anthropic", "DATABASE_URL": "postgresql://x/y"})
        self.assertEqual(missing, ["ANTHROPIC_API_KEY"])

    def test_database_url_is_still_required(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_CONFIG_DIR": self.empty()}, clear=True):
            missing = install.missing_required({"DEWEY_STACK": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-x"})
        self.assertEqual(missing, ["DATABASE_URL"])

    def empty(self):
        root = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "configs").mkdir()
        return str(root)


if __name__ == "__main__":
    unittest.main()

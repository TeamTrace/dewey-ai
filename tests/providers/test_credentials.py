"""Which Claude credential build_stack() accepts for DEWEY_STACK=anthropic.

anthropic_llm.py constructs Anthropic() with no arguments, so any source the SDK
resolves works: an API key, an auth token, Workload Identity Federation, or a
profile written by `ant auth login`. These tests pin that, and pin the two ways
the configuration can be wrong in a way the SDK would only report at request time.

Every test sets ANTHROPIC_CONFIG_DIR, so none of them can see the developer's own
profile directory.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "app"))

import json
import os
import tempfile
import unittest
from unittest import mock

import providers
from models import NewsroomConfig

NEWSROOM = NewsroomConfig(publisher="The Example Times")
BASE_ENV = {"DEWEY_STACK": "anthropic", "DATABASE_URL": "postgresql://u:p@localhost/dewey"}
FEDERATION = {
    "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_01",
    "ANTHROPIC_ORGANIZATION_ID": "00000000-0000-0000-0000-000000000000",
    "ANTHROPIC_IDENTITY_TOKEN_FILE": "/var/run/secrets/anthropic.com/token",
}


class ConfigDirMixin:
    def config_dir(self, profiles=(), active=None):
        """A config directory laid out the way the SDK expects, cleaned up with the test."""
        root = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "configs").mkdir()
        for name in profiles:
            (root / "configs" / f"{name}.json").write_text(
                json.dumps({"version": "1.0", "organization_id": "org"}))
        if active is not None:
            (root / "active_config").write_text(active + "\n")
        return str(root)


class CredentialSourceTest(ConfigDirMixin, unittest.TestCase):

    def source(self, config_dir=None, **env):
        env.setdefault("ANTHROPIC_CONFIG_DIR", config_dir or self.config_dir())
        with mock.patch.dict(os.environ, env, clear=True):
            return providers.anthropic_credential_source()

    def test_api_key_wins(self):
        self.assertEqual(self.source(ANTHROPIC_API_KEY="sk-ant-x", ANTHROPIC_AUTH_TOKEN="t"),
                         "ANTHROPIC_API_KEY")

    def test_auth_token_alone(self):
        self.assertEqual(self.source(ANTHROPIC_AUTH_TOKEN="sk-ant-oat01-x"), "ANTHROPIC_AUTH_TOKEN")

    def test_federation_needs_all_three(self):
        self.assertEqual(self.source(**FEDERATION), "workload identity federation")
        for dropped in FEDERATION:
            partial = {k: v for k, v in FEDERATION.items() if k != dropped}
            self.assertEqual(self.source(**partial), "", f"accepted federation without {dropped}")

    def test_inline_identity_token_also_counts(self):
        env = {k: v for k, v in FEDERATION.items() if k != "ANTHROPIC_IDENTITY_TOKEN_FILE"}
        self.assertEqual(self.source(**env, ANTHROPIC_IDENTITY_TOKEN="eyJhbGciOi"),
                         "workload identity federation")

    def test_nothing_set(self):
        self.assertEqual(self.source(), "")

    def test_profile_from_active_config(self):
        d = self.config_dir(profiles=["work"], active="work")
        self.assertEqual(self.source(config_dir=d), "profile")

    def test_profile_named_default_without_active_config(self):
        self.assertEqual(self.source(config_dir=self.config_dir(profiles=["default"])), "profile")

    def test_active_config_naming_a_missing_profile_falls_back_to_default(self):
        d = self.config_dir(profiles=["default"], active="gone")
        self.assertEqual(self.source(config_dir=d), "profile")

    def test_anthropic_profile_env_var_selects_it(self):
        d = self.config_dir(profiles=["staging"])
        self.assertEqual(self.source(config_dir=d, ANTHROPIC_PROFILE="staging"), "profile")

    def test_named_profile_that_does_not_exist_is_not_a_fallthrough(self):
        d = self.config_dir(profiles=["default"])
        self.assertEqual(self.source(config_dir=d, ANTHROPIC_PROFILE="missing"), "")

    def test_blank_values_do_not_count_as_credentials(self):
        self.assertEqual(self.source(ANTHROPIC_API_KEY="   "), "")


class BuildStackTest(ConfigDirMixin, unittest.TestCase):
    """The gate in build_stack(), with the store and the Claude client stubbed out."""

    def build(self, config_dir=None, **env):
        env.setdefault("ANTHROPIC_CONFIG_DIR", config_dir or self.config_dir())
        with mock.patch.dict(os.environ, {**BASE_ENV, **env}, clear=True), \
                mock.patch.object(providers, "build_pgvector_store", return_value=object()), \
                mock.patch("providers.anthropic_llm.AnthropicLLM") as llm:
            providers.build_stack(NEWSROOM)
        return llm

    def test_a_profile_is_enough(self):
        llm = self.build(config_dir=self.config_dir(profiles=["default"]))
        self.assertTrue(llm.called, "build_stack rejected a profile-only configuration")

    def test_api_key_still_works(self):
        self.assertTrue(self.build(ANTHROPIC_API_KEY="sk-ant-x").called)

    def test_no_credential_at_all_is_an_error(self):
        with self.assertRaises(providers.StackConfigError) as ctx:
            self.build()
        self.assertIn("ant auth login", str(ctx.exception))

    def test_blank_api_key_is_reported_rather_than_shadowing_a_profile(self):
        d = self.config_dir(profiles=["default"])
        with self.assertRaises(providers.StackConfigError) as ctx:
            self.build(config_dir=d, ANTHROPIC_API_KEY="")
        self.assertIn("set but empty", str(ctx.exception))

    def test_database_url_is_still_required(self):
        with self.assertRaises(providers.StackConfigError) as ctx:
            with mock.patch.dict(os.environ, {"DEWEY_STACK": "anthropic",
                                              "ANTHROPIC_API_KEY": "sk-ant-x",
                                              "ANTHROPIC_CONFIG_DIR": self.config_dir()},
                                 clear=True):
                providers.build_stack(NEWSROOM)
        self.assertIn("DATABASE_URL", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

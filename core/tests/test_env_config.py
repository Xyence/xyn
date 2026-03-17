import os
import unittest
from unittest.mock import patch

from core.env_config import export_runtime_env, load_seed_config


class SeedEnvConfigTests(unittest.TestCase):
    def _base_env(self) -> dict[str, str]:
        return {
            "XYN_ENV": "local",
            "XYN_AUTH_MODE": "dev",
            "XYN_INTERNAL_TOKEN": "test-token",
            "DATABASE_URL": "postgresql://xyn:xyn_dev_password@postgres:5432/xyn",
            "REDIS_URL": "redis://redis:6379/0",
        }

    def test_infers_provider_from_single_key(self):
        env = self._base_env()
        env["XYN_OPENAI_API_KEY"] = "sk-test-openai"
        with patch("core.env_config._load_seed_dotenv_once", return_value=None), patch.dict(os.environ, env, clear=True):
            config = load_seed_config()
        self.assertTrue(config.ai_enabled)
        self.assertEqual(config.ai_provider, "openai")
        self.assertEqual(config.ai_model, "gpt-5-mini")

    def test_defaults_to_openai_when_multiple_keys_include_openai(self):
        env = self._base_env()
        env["XYN_OPENAI_API_KEY"] = "sk-test-openai"
        env["XYN_GEMINI_API_KEY"] = "gem-test"
        with patch("core.env_config._load_seed_dotenv_once", return_value=None), patch.dict(os.environ, env, clear=True):
            config = load_seed_config()
        self.assertEqual(config.ai_provider, "openai")
        self.assertTrue(config.ai_enabled)

    def test_requires_provider_when_multiple_non_openai_keys_present(self):
        env = self._base_env()
        env["XYN_GEMINI_API_KEY"] = "gem-test"
        env["XYN_ANTHROPIC_API_KEY"] = "ant-test"
        with patch("core.env_config._load_seed_dotenv_once", return_value=None), patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                load_seed_config()

    def test_explicit_provider_requires_matching_key(self):
        env = self._base_env()
        env["XYN_AI_PROVIDER"] = "anthropic"
        with patch("core.env_config._load_seed_dotenv_once", return_value=None), patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                load_seed_config()

    def test_disables_ai_when_no_keys(self):
        with patch("core.env_config._load_seed_dotenv_once", return_value=None), patch.dict(os.environ, self._base_env(), clear=True):
            config = load_seed_config()
        self.assertFalse(config.ai_enabled)
        self.assertEqual(config.ai_provider, "none")
        self.assertEqual(config.ai_model, "none")

    def test_exports_optional_planning_and_coding_overlays(self):
        env = self._base_env()
        env["XYN_OPENAI_API_KEY"] = "sk-test-openai"
        env["XYN_AI_PLANNING_PROVIDER"] = "anthropic"
        env["XYN_AI_PLANNING_MODEL"] = "claude-3-7-sonnet-latest"
        env["XYN_AI_PLANNING_API_KEY"] = "plan-key"
        env["XYN_AI_CODING_PROVIDER"] = "gemini"
        env["XYN_AI_CODING_MODEL"] = "gemini-2.0-flash"
        env["XYN_AI_CODING_API_KEY"] = "code-key"
        with patch("core.env_config._load_seed_dotenv_once", return_value=None), patch.dict(os.environ, env, clear=True):
            config = load_seed_config()
            exported = export_runtime_env(config)
        self.assertEqual(exported["XYN_AI_PLANNING_PROVIDER"], "anthropic")
        self.assertEqual(exported["XYN_AI_PLANNING_MODEL"], "claude-3-7-sonnet-latest")
        self.assertEqual(exported["XYN_AI_PLANNING_API_KEY"], "plan-key")
        self.assertEqual(exported["XYN_AI_CODING_PROVIDER"], "gemini")
        self.assertEqual(exported["XYN_AI_CODING_MODEL"], "gemini-2.0-flash")
        self.assertEqual(exported["XYN_AI_CODING_API_KEY"], "code-key")

    def test_requires_complete_planning_overlay(self):
        env = self._base_env()
        env["XYN_OPENAI_API_KEY"] = "sk-test-openai"
        env["XYN_AI_PLANNING_PROVIDER"] = "openai"
        with patch("core.env_config._load_seed_dotenv_once", return_value=None), patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                load_seed_config()

    def test_exports_managed_storage_roots_with_defaults_and_overrides(self):
        env = self._base_env()
        env["XYN_OPENAI_API_KEY"] = "sk-test-openai"
        env["XYN_ARTIFACT_ROOT"] = "/srv/xyn/artifacts"
        env["XYN_WORKSPACE_ROOT"] = "/srv/xyn/workspaces"
        env["XYN_WORKSPACE_RETENTION_DAYS"] = "21"
        with patch("core.env_config._load_seed_dotenv_once", return_value=None), patch.dict(os.environ, env, clear=True):
            config = load_seed_config()
            exported = export_runtime_env(config)
        self.assertEqual(exported["XYN_ARTIFACT_ROOT"], "/srv/xyn/artifacts")
        self.assertEqual(exported["ARTIFACT_STORE_PATH"], "/srv/xyn/artifacts")
        self.assertEqual(exported["XYN_WORKSPACE_ROOT"], "/srv/xyn/workspaces")
        self.assertEqual(exported["XYN_LOCAL_WORKSPACE_ROOT"], "/srv/xyn/workspaces")
        self.assertEqual(exported["XYNSEED_WORKSPACE"], "/srv/xyn/workspaces")
        self.assertEqual(exported["XYN_WORKSPACE_RETENTION_DAYS"], "21")


if __name__ == "__main__":
    unittest.main()

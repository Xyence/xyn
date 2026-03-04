#!/usr/bin/env python3
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import unittest


def _load_xynctl_module():
    path = Path(__file__).resolve().parents[1] / "xynctl"
    loader = SourceFileLoader("xynctl_module", str(path))
    spec = importlib.util.spec_from_loader("xynctl_module", loader)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load xynctl module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class XynCtlEnvTests(unittest.TestCase):
    def test_normalize_accepts_new_style_key(self):
        mod = _load_xynctl_module()
        env = {"XYN_OPENAI_API_KEY": "sk-test"}
        normalized = mod.normalize_ai_env(env)
        self.assertEqual(normalized.get("XYN_OPENAI_API_KEY"), "sk-test")
        self.assertEqual(normalized.get("OPENAI_API_KEY"), "sk-test")
        self.assertEqual(normalized.get("XYN_AI_PROVIDER"), "openai")

    def test_normalize_maps_legacy_key(self):
        mod = _load_xynctl_module()
        env = {"OPENAI_API_KEY": "sk-legacy"}
        normalized = mod.normalize_ai_env(env)
        self.assertEqual(normalized.get("XYN_OPENAI_API_KEY"), "sk-legacy")
        self.assertEqual(normalized.get("OPENAI_API_KEY"), "sk-legacy")

    def test_ai_disabled_sets_provider_none(self):
        mod = _load_xynctl_module()
        env = {"XYN_AI_DISABLED": "true"}
        normalized = mod.normalize_ai_env(env)
        self.assertEqual(normalized.get("XYN_AI_PROVIDER"), "none")

    def test_artifact_defaults_are_added(self):
        mod = _load_xynctl_module()
        normalized = mod.normalize_ai_env({})
        self.assertEqual(normalized.get("XYN_ARTIFACT_REGISTRY"), "public.ecr.aws/i0h0h0n4/xyn/artifacts")
        self.assertEqual(normalized.get("XYN_UI_IMAGE"), "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-ui:dev")
        self.assertEqual(normalized.get("XYN_API_IMAGE"), "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:dev")


if __name__ == "__main__":
    unittest.main()

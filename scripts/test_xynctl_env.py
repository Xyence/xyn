#!/usr/bin/env python3
import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


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

    def test_explicit_image_overrides_take_precedence(self):
        mod = _load_xynctl_module()
        payload = {
            "images": {
                "ui_image": "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-ui:develop",
                "api_image": "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:develop",
                "channel": "develop",
            }
        }
        env = {"XYN_API_IMAGE": "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:83915e4"}
        merged = mod._apply_explicit_image_overrides(payload, env)
        images = merged.get("images") or {}
        self.assertEqual(images.get("api_image"), "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:83915e4")
        self.assertEqual(images.get("ui_image"), "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-ui:develop")

    def test_build_context_pack_distribution_artifact_includes_revision_identity(self):
        mod = _load_xynctl_module()
        manifest = {
            "source_system": "xyn-platform",
            "source_seed_pack_slug": "xyn-core-context-packs",
            "source_seed_pack_version": "v1.2.0",
            "context_packs": [{"slug": "xyn-console-default", "content": "# test"}],
        }
        artifact = mod._build_context_pack_distribution_artifact(manifest)
        self.assertEqual(artifact["artifact_schema"], "xyn.context-pack-artifact.v1")
        self.assertEqual(artifact["artifact"]["slug"], "xyn-core-context-packs")
        self.assertEqual(artifact["artifact"]["lineage_id"], "xyn-core-context-packs")
        self.assertTrue(str(artifact["artifact"]["revision_id"]).startswith("ctx-"))
        self.assertEqual(artifact["artifact"]["version_label"], "v1.2.0")
        self.assertEqual(len(artifact["content"]["context_packs"]), 1)

    def test_write_context_pack_distribution_artifact_from_manifest(self):
        mod = _load_xynctl_module()
        manifest = {
            "source_system": "xyn-platform",
            "source_seed_pack_slug": "xyn-core-context-packs",
            "source_seed_pack_version": "v1.2.0",
            "context_packs": [{"slug": "xyn-console-default", "content": "# test"}],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "context-packs.manifest.json"
            artifact_path = Path(tmpdir) / "context-packs.artifact.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            written = mod._write_context_pack_distribution_artifact(manifest_path=manifest_path, artifact_path=artifact_path)
            self.assertTrue(written)
            self.assertTrue(artifact_path.exists())
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["artifact"]["kind"], "context-pack-bundle")

    def test_running_ui_api_containers_filters_local_and_sibling(self):
        mod = _load_xynctl_module()
        with mock.patch.object(
            mod,
            "_run_capture",
            return_value=(
                0,
                "\n".join(
                    [
                        "xyn-local-api\txyn-api:local",
                        "xyn-local-ui\txyn-ui:local",
                        "xyn-smoke-real-estate-de-api\txyn-api",
                        "xyn-smoke-real-estate-de-ui\txyn-ui",
                        "xyn-core\txyn-core",
                    ]
                ),
                "",
            ),
        ):
            rows = mod._running_ui_api_containers()
        names = {row["name"] for row in rows}
        self.assertIn("xyn-local-api", names)
        self.assertIn("xyn-local-ui", names)
        self.assertIn("xyn-smoke-real-estate-de-api", names)
        self.assertIn("xyn-smoke-real-estate-de-ui", names)
        self.assertNotIn("xyn-core", names)

    def test_freshness_check_fails_on_stale_or_unverifiable_images(self):
        mod = _load_xynctl_module()
        with mock.patch.object(mod, "_expected_platform_image_source_shas", return_value=("sha-api", "sha-ui")), \
            mock.patch.object(
                mod,
                "_running_ui_api_containers",
                return_value=[
                    {"name": "xyn-local-api", "image": "xyn-api:local", "service": "api"},
                    {"name": "xyn-local-ui", "image": "xyn-ui:local", "service": "ui"},
                ],
            ), \
            mock.patch.object(
                mod,
                "_image_source_sha",
                side_effect=lambda image: {"xyn-api:local": "sha-api", "xyn-ui:local": "unknown"}.get(image, ""),
            ):
            status = mod.freshness_check({})
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()

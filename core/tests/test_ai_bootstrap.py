import os
import unittest
from unittest.mock import patch

from core.ai_bootstrap import ensure_default_agent_via_api


class AiBootstrapTests(unittest.TestCase):
    def test_skips_when_base_url_is_seed_loopback(self):
        env = {
            "XYN_API_BASE_URL": "http://localhost:8000",
            "XYN_INTERNAL_TOKEN": "test-token",
        }
        with patch.dict(os.environ, env, clear=True):
            outcome = ensure_default_agent_via_api()
        self.assertEqual(outcome, "unsupported")

    def test_skips_when_base_url_is_seed_loopback_without_port(self):
        env = {
            "XYN_API_BASE_URL": "http://127.0.0.1",
            "XYN_INTERNAL_TOKEN": "test-token",
        }
        with patch.dict(os.environ, env, clear=True):
            outcome = ensure_default_agent_via_api()
        self.assertEqual(outcome, "unsupported")


if __name__ == "__main__":
    unittest.main()

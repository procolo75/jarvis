"""
tests/test_providers.py
───────────────────────
Model providers: Cloudflare's account id in the base URL, and the per-provider
limits that replaced the single `tpm_limit`.

    cd jarvis && python3 -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "jarvis"))

from web import store                                             # noqa: E402


class BaseUrl(unittest.TestCase):

    def test_cloudflare_carries_the_account_id(self):
        self.assertEqual(
            store.provider_base_url("cloudflare", {"cloudflare_account_id": " abc123 "}),
            "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1")

    def test_others_are_unchanged(self):
        self.assertEqual(store.provider_base_url("groq", {}), "https://api.groq.com/openai/v1")

    def test_unknown_falls_back_to_the_first(self):
        self.assertEqual(store.provider_base_url("nope", {}), store.PROVIDERS[0]["base_url"])


class LimitsMigration(unittest.TestCase):
    """A pre-4.17 global tpm_limit goes to the provider it was tuned against."""

    def test_moves_to_the_primary_provider(self):
        out = store.migrate_provider_limits({"provider": "groq", "tpm_limit": 30000})
        self.assertEqual(out["provider_limits"], {"groq": {"tpm": 30000}})

    def test_zero_means_nothing_to_move(self):
        self.assertNotIn("provider_limits", store.migrate_provider_limits({"tpm_limit": 0}))

    def test_never_lands_on_cloudflare(self):
        # The 8000 was Groq's; switching the primary to Cloudflare must not move it there.
        out = store.migrate_provider_limits(
            {"provider": "cloudflare", "fallback_provider": "groq", "tpm_limit": 8000})
        self.assertEqual(out["provider_limits"], {"groq": {"tpm": 8000}})
        out = store.migrate_provider_limits({"provider": "cloudflare", "tpm_limit": 8000})
        self.assertEqual(out["provider_limits"], {})

    def test_persisted_once_and_old_key_dropped(self):
        import json, tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "s.json")
            f.write_text(json.dumps({"provider": "groq", "tpm_limit": 30000}))
            with mock.patch.object(store, "SETTINGS_FILE", f):
                self.assertTrue(store.persist_provider_limits())
                saved = json.loads(f.read_text())
                self.assertEqual(saved["provider_limits"], {"groq": {"tpm": 30000}})
                self.assertNotIn("tpm_limit", saved)
                self.assertFalse(store.persist_provider_limits())

    def test_saved_limits_win(self):
        data = {"provider": "groq", "tpm_limit": 30000, "provider_limits": {}}
        self.assertEqual(store.migrate_provider_limits(data)["provider_limits"], {})


class Models(unittest.TestCase):

    def test_every_cloudflare_model_has_a_known_window(self):
        import core
        for m in store.CLOUDFLARE_MODELS:
            self.assertGreater(core.context_window_for(m["id"]), core.DEFAULT_CONTEXT_WINDOW,
                               m["id"])


if __name__ == "__main__":
    unittest.main()

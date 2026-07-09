import unittest

from app.control.model import registry
from app.products._account_selection import mode_candidates


class ConsoleModelSelectionTests(unittest.TestCase):
    def test_console_models_use_existing_virtual_quota_buckets(self):
        cases = {
            "grok-4.20-0309-reasoning-console": 2,
            "grok-4.20-multi-agent-console": 3,
            "grok-4.3-console": 4,
            "grok-4.5-console": 4,
        }
        for model, expected_mode in cases.items():
            with self.subTest(model=model):
                self.assertEqual(mode_candidates(registry.resolve(model)), (expected_mode,))


if __name__ == "__main__":
    unittest.main()

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from asset_hub.contracts import CallbackStatus, ProviderCallback


class CallbackContractTest(unittest.TestCase):
    def test_callback_sample_supports_out_of_order_events(self):
        rows = json.loads((Path(__file__).parents[1] / "fixtures" / "callbacks.json").read_text(encoding="utf-8"))
        events = [ProviderCallback.from_dict(row) for row in rows]
        self.assertEqual(events[1].status, CallbackStatus.SUCCEEDED)
        self.assertLess(events[1].occurred_at, events[0].occurred_at)
        self.assertEqual(events[1].attributes["width"], 1080)


if __name__ == "__main__":
    unittest.main()

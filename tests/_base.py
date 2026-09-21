"""测试公共夹具（被各 test_*.py 以顶层模块方式导入）。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from asset_hub import AssetHub, Inbox, Lineage, Store
from asset_hub.media import synthetic_png


class HubCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = Store(self.root)
        self.hub = AssetHub(self.store)
        self.inbox = Inbox(self.root / "inbox")
        self.lineage = Lineage(self.hub)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def seed_shot(self, shot_id="shot-1", code="S010", *, costume="红色风衣"):
        self.hub.add_shot(shot_id, code, "雨夜天桥", "主角转身特写")
        self.hub.add_prompt_package(
            shot_id, f"pkg-{shot_id}",
            "电影感特写，角色服装：{costume}；情绪：{mood}",
            {"costume": costume, "mood": "克制"},
        )
        ref = self.hub.add_reference(
            "ref-hero", "hero_costume", synthetic_png(8, 16, (180, 30, 30)),
            shot_id=None, created_by="art-director",
        )
        self.hub.bind_reference(shot_id, "ref-hero", "hero_costume")
        return ref

    def prepare_dispatch_ok(self, shot_id="shot-1", adapter="echo-image"):
        prep = self.hub.prepare_generation(shot_id, adapter, {"steps": 20}, "image/png")
        d = self.hub.dispatch(prep["attempt_id"])
        return prep["attempt_id"], d
